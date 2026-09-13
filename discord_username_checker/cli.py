"""Interface en ligne de commande."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from typing import List, Optional

from . import __version__
from .checker import DEFAULT_ENDPOINT, DEFAULT_USER_AGENT, UsernameChecker
from .generator import CHARSETS, build_candidates, validate_username
from .ratelimit import RateLimiter
from .runner import Runner, format_duration
from .storage import ResultStore
from .webhook import AvailableNotifier, DiscordWebhook, mask_webhook_url, validate_webhook_url

ENV_WEBHOOK = "DISCORD_WEBHOOK_URL"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FATAL = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discord-username-checker",
        description=(
            "Vérifie en masse la disponibilité des pseudos Discord (4 caractères par défaut) "
            "et envoie ceux qui sont libres sur un webhook Discord."
        ),
        epilog=(
            "Exemples :\n"
            "  %(prog)s abcd wxyz              vérifie uniquement ces pseudos\n"
            "  %(prog)s --charset letters      toutes les combinaisons de 4 lettres (456 976)\n"
            "  %(prog)s --pattern 'a??z'       tous les pseudos a..z\n"
            "  %(prog)s --wordlist mots.txt    les pseudos d'un fichier (un par ligne)\n"
            "  %(prog)s --shuffle --limit 500  500 pseudos aléatoires\n\n"
            f"Le webhook peut aussi être fourni par la variable d'environnement {ENV_WEBHOOK}."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("usernames", nargs="*", help="pseudos précis à vérifier (optionnel)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    source = parser.add_argument_group("choix des pseudos")
    source.add_argument("-l", "--length", type=int, default=4, help="longueur des pseudos générés (défaut : 4)")
    source.add_argument(
        "-c",
        "--charset",
        default="letters",
        help=(
            "jeu de caractères : " + ", ".join(CHARSETS) + " ou une liste de caractères "
            "(défaut : letters, soit a-z)"
        ),
    )
    source.add_argument("-p", "--pattern", help="motif avec ? comme joker, ex. 'a??z' ou '??_?'")
    source.add_argument("-w", "--wordlist", help="fichier de pseudos à tester (un par ligne)")
    source.add_argument("--shuffle", action="store_true", help="ordre aléatoire")
    source.add_argument("--seed", type=int, help="graine pour --shuffle (résultats reproductibles)")
    source.add_argument("--limit", type=int, help="nombre maximum de pseudos à tester")

    speed = parser.add_argument_group("vitesse et réseau")
    speed.add_argument("--workers", type=int, default=2, help="threads simultanés (défaut : 2)")
    speed.add_argument("--rps", type=float, default=2.0, help="requêtes par seconde au total (défaut : 2)")
    speed.add_argument("--timeout", type=float, default=15.0, help="délai HTTP en secondes (défaut : 15)")
    speed.add_argument("--max-retries", type=int, default=3, help="nouveaux essais sur erreur réseau (défaut : 3)")
    speed.add_argument("--proxy", help="proxy HTTP(S), ex. http://127.0.0.1:8080")
    speed.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="URL de l'API à interroger")
    speed.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent envoyé")
    speed.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="'Nom: valeur'",
        help="en-tête HTTP supplémentaire (répétable)",
    )
    speed.add_argument(
        "--abort-after-problems",
        type=int,
        default=20,
        help="arrêt après N erreurs consécutives, 0 pour désactiver (défaut : 20)",
    )

    output = parser.add_argument_group("sorties")
    output.add_argument("-o", "--output-dir", default="resultats", help="dossier des fichiers de sortie (défaut : resultats)")
    output.add_argument("--no-resume", action="store_true", help="ne pas ignorer les pseudos déjà vérifiés")
    output.add_argument("--progress-interval", type=float, default=15.0, help="secondes entre deux lignes de progression")
    output.add_argument("-v", "--verbose", action="store_true", help="affiche chaque résultat")
    output.add_argument("--dry-run", action="store_true", help="affiche ce qui serait testé, sans appeler Discord")

    hook = parser.add_argument_group("webhook Discord")
    hook.add_argument("--webhook", help=f"URL du webhook (ou variable {ENV_WEBHOOK})")
    hook.add_argument("--webhook-name", default="Discord Username Checker", help="nom affiché du bot")
    hook.add_argument("--webhook-batch", type=int, default=25, help="envoyer dès N pseudos disponibles (défaut : 25)")
    hook.add_argument("--webhook-interval", type=float, default=60.0, help="envoyer au plus tard toutes les N secondes (défaut : 60)")
    hook.add_argument("--no-summary", action="store_true", help="pas de message récapitulatif à la fin")
    hook.add_argument("--test-webhook", action="store_true", help="envoie un message de test puis quitte")
    return parser


def parse_headers(values: List[str]) -> dict:
    headers = {}
    for raw in values:
        if ":" not in raw:
            raise ValueError(f"en-tête invalide (attendu 'Nom: valeur') : {raw!r}")
        name, value = raw.split(":", 1)
        headers[name.strip()] = value.strip()
    return headers


def resolve_webhook_url(argument: Optional[str]) -> Optional[str]:
    url = (argument or os.environ.get(ENV_WEBHOOK) or "").strip()
    return url or None


def configure_logging(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("discord_username_checker")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = configure_logging(args.verbose)

    if args.length < 2 or args.length > 32:
        parser.error("--length doit être compris entre 2 et 32")
    if args.workers < 1:
        parser.error("--workers doit être >= 1")
    if args.rps <= 0:
        parser.error("--rps doit être > 0")
    if args.pattern and args.wordlist:
        parser.error("--pattern et --wordlist sont exclusifs")

    try:
        extra_headers = parse_headers(args.header)
    except ValueError as error:
        parser.error(str(error))

    webhook_url = resolve_webhook_url(args.webhook)
    webhook: Optional[DiscordWebhook] = None
    if webhook_url:
        if not validate_webhook_url(webhook_url):
            logger.error("URL de webhook invalide : attendu https://discord.com/api/webhooks/<id>/<token>")
            return EXIT_ERROR
        webhook = DiscordWebhook(
            webhook_url, bot_name=args.webhook_name, timeout=args.timeout, proxy=args.proxy, logger=logger
        )
        logger.info("Webhook configuré : %s", mask_webhook_url(webhook_url))
    elif not args.dry_run:
        logger.warning(
            "Aucun webhook (option --webhook ou variable %s) : les pseudos disponibles "
            "seront seulement écrits dans le dossier de sortie.",
            ENV_WEBHOOK,
        )

    if args.test_webhook:
        if webhook is None:
            logger.error("--test-webhook nécessite un webhook")
            return EXIT_ERROR
        ok = webhook.send("🔔 Test : le vérificateur de pseudos Discord est bien connecté à ce webhook.")
        logger.info("Message de test %s", "envoyé" if ok else "refusé")
        return EXIT_OK if ok else EXIT_ERROR

    explicit = [n for n in args.usernames if n.strip()]
    for name in explicit:
        reason = validate_username(name.strip().lower())
        if reason:
            logger.warning("Pseudo ignoré car invalide (%s) : %s", reason, name)
    explicit = [n for n in explicit if validate_username(n.strip().lower()) is None]
    if args.usernames and not explicit:
        logger.error("Aucun pseudo valide à vérifier.")
        return EXIT_ERROR

    try:
        candidates, total = build_candidates(
            length=args.length,
            charset=args.charset,
            pattern=args.pattern,
            wordlist=args.wordlist,
            explicit=explicit or None,
            shuffle=args.shuffle,
            seed=args.seed,
            limit=args.limit,
        )
    except (ValueError, OSError) as error:
        logger.error("Impossible de préparer la liste de pseudos : %s", error)
        return EXIT_ERROR

    # Filtre local : évite d'envoyer à Discord des pseudos forcément invalides.
    candidates = (name for name in candidates if validate_username(name) is None)

    if args.dry_run:
        shown = 0
        count = 0
        for name in candidates:
            count += 1
            if shown < 25:
                print(name)
                shown += 1
        if count > shown:
            print(f"… et {count - shown} autres")
        print(f"Total : {count} pseudo(s) à tester (estimation initiale : {total})")
        if total and args.rps:
            print(f"Durée estimée à {args.rps:g} req/s : {format_duration(count / args.rps)}")
        return EXIT_OK

    store = ResultStore(
        os.path.join(args.output_dir, "resultats.csv"),
        os.path.join(args.output_dir, "disponibles.txt"),
        logger=logger,
    )
    already_checked = set()
    if not args.no_resume and not explicit:
        already_checked = store.load_checked()
        if already_checked:
            logger.info("Reprise : %d pseudo(s) déjà vérifié(s) seront ignorés", len(already_checked))
    try:
        store.open()
    except OSError as error:
        logger.error("Impossible d'ouvrir les fichiers de sortie : %s", error)
        return EXIT_ERROR

    notifier: Optional[AvailableNotifier] = None
    if webhook is not None:
        notifier = AvailableNotifier(
            webhook,
            batch_size=args.webhook_batch,
            flush_interval=args.webhook_interval,
            logger=logger,
        )
        notifier.start()

    stop_event = threading.Event()

    def _on_signal(signum, _frame):  # pragma: no cover - dépend du système
        logger.warning("Signal %s reçu : arrêt propre en cours…", signum)
        stop_event.set()

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass

    checker = UsernameChecker(
        endpoint=args.endpoint,
        rate_limiter=RateLimiter(args.rps),
        timeout=args.timeout,
        max_retries=args.max_retries,
        user_agent=args.user_agent,
        extra_headers=extra_headers,
        proxy=args.proxy,
        logger=logger,
    )
    runner = Runner(
        checker,
        candidates,
        total,
        workers=args.workers,
        store=store,
        notifier=notifier,
        already_checked=already_checked,
        abort_after_problems=args.abort_after_problems,
        progress_interval=args.progress_interval,
        log_every_result=args.verbose or bool(explicit),
        logger=logger,
        stop_event=stop_event,
    )

    logger.info(
        "Démarrage : %s pseudo(s) à tester, %d thread(s), %.2g req/s max, résultats dans %s",
        total if total is not None else "?",
        args.workers,
        args.rps,
        os.path.abspath(args.output_dir),
    )

    try:
        stats = runner.run()
    finally:
        if notifier is not None:
            notifier.stop()
        store.close()

    logger.info("Terminé : %s", stats.summary())
    if checker.rate_limit_hits:
        logger.info("Limites de débit rencontrées (429) : %d", checker.rate_limit_hits)
    if notifier is not None and notifier.failed:
        logger.warning("%d pseudo(s) disponible(s) n'ont pas pu être envoyés au webhook", notifier.failed)

    if notifier is not None and not args.no_summary and stats.checked:
        status_line = "🏁 Vérification terminée"
        if stats.fatal is not None:
            status_line = "⛔ Vérification arrêtée : " + stats.fatal.detail
        elif stats.aborted_reason:
            status_line = "⛔ Vérification arrêtée : " + stats.aborted_reason
        elif stop_event.is_set():
            status_line = "⏹️ Vérification interrompue"
        notifier.send_text(f"{status_line}\n{stats.summary()}")

    if stats.fatal is not None or stats.aborted_reason:
        return EXIT_FATAL
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

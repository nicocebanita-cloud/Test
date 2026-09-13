#!/usr/bin/env python3
"""Lanceur tout-en-un du vérificateur de pseudos Discord.

- Premier lancement : assistant qui demande l'URL du webhook, la teste et l'enregistre
  dans ``config.json`` (à côté de ce fichier). Rien à retaper ensuite.
- Lancements suivants : la vérification massive démarre directement et reprend là où
  elle s'était arrêtée.
- Relance automatique après une erreur, un plantage ou un blocage de Discord
  (captcha, 403), avec un temps de pause avant chaque nouvelle tentative.
- Tout ce qui s'affiche est aussi écrit dans ``resultats/journal.log``.

Utilisation : double-cliquez sur ``Lancer.bat`` (Windows) ou lancez ``./lancer.sh``
(Mac, Linux), ou encore ``python lancer.py``. Ctrl+C arrête proprement.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

EXIT_FATAL_CHECKER = 2  # code renvoyé par le vérificateur sur captcha, 403 ou erreurs en série

BRANCHE = "claude/discord-4-letter-username-checker-c8lyhz"
UPDATE_ZIP_URL = f"https://github.com/nicocebanita-cloud/Test/archive/refs/heads/{BRANCHE}.zip"
# Jamais écrasés par une mise à jour : la configuration, la liste et les résultats de l'utilisateur.
FICHIERS_PRESERVES = {"config.json", "resultats", "pseudos.txt"}
PSEUDOS_PATH = ROOT / "pseudos.txt"

MODELE_PSEUDOS = """\
# Liste des pseudos Discord à surveiller : un par ligne, en minuscules.
# Autorisé : lettres a-z, chiffres, _ et . (pas deux points de suite).
# Les lignes qui commencent par # sont ignorées.
#
# Discord n'autorise qu'environ 5 vérifications par heure et par adresse IP :
# une liste de 20 pseudos est parcourue en 4 h environ, puis re-vérifiée chaque jour.
# Vous serez prévenu sur le webhook dès qu'un pseudo de la liste se libère.
#
# Exemples (retirez le # pour les activer) :
# nova
# zed_
# k.o.
"""

DEFAULT_CONFIG: Dict[str, object] = {
    "_aide": (
        "webhook : URL du webhook Discord. charset : letters, alnum ou full. "
        "length : longueur des pseudos. rps : requêtes par seconde. workers : threads. "
        "pattern : motif avec ? (vide = toutes les combinaisons). limit : 0 = pas de limite. "
        "pause_blocage_minutes : attente après un blocage Discord. "
        "pause_erreur_secondes : attente après une erreur. "
        "relances_max_blocage / relances_max_erreur : nombre de tentatives avant abandon. "
        "mode : surveillance (liste pseudos.txt re-vérifiée toutes les recheck_heures) ou massif."
    ),
    "mode": "surveillance",
    "pseudos": "pseudos.txt",
    "recheck_heures": 24,
    "webhook": "",
    "charset": "letters",
    "length": 4,
    "rps": 2,
    "workers": 2,
    "pattern": "",
    "limit": 0,
    "webhook_batch": 25,
    "webhook_interval": 60,
    "output_dir": "resultats",
    "pause_blocage_minutes": 30,
    "pause_erreur_secondes": 60,
    "relances_max_blocage": 10,
    "relances_max_erreur": 5,
}

ACTION_TERMINE = "termine"
ACTION_ARRET = "arret"
ACTION_RELANCE = "relance"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def charger_config(path: Optional[Path] = None) -> Optional[Dict[str, object]]:
    path = path or CONFIG_PATH
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        print(f"⚠️  {path.name} est illisible ({error}) : l'assistant va le recréer.")
        return None
    if not isinstance(data, dict):
        return None
    config = dict(DEFAULT_CONFIG)
    config.update(data)
    return config


def enregistrer_config(config: Dict[str, object], path: Optional[Path] = None) -> None:
    path = path or CONFIG_PATH
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
    try:
        os.chmod(path, 0o600)  # le fichier contient le token du webhook
    except OSError:
        pass


def _demander(question: str, defaut: str) -> str:
    reponse = input(f"{question} [{defaut}] : ").strip()
    return reponse or defaut


def assistant_configuration(existante: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """Pose quelques questions et renvoie une configuration complète."""
    from discord_username_checker.webhook import validate_webhook_url

    config = dict(DEFAULT_CONFIG)
    if existante:
        config.update(existante)

    print()
    print("=" * 64)
    print("  Configuration du vérificateur de pseudos Discord")
    print("=" * 64)
    print()
    print("Il faut l'URL d'un webhook Discord. Pour en créer un :")
    print("  1. Dans Discord, ouvrez les paramètres du salon qui recevra les pseudos")
    print("  2. Intégrations → Webhooks → Nouveau webhook")
    print("  3. « Copier l'URL du webhook »")
    print()

    while True:
        defaut = str(config.get("webhook") or "")
        invite = "Collez l'URL du webhook"
        if defaut:
            invite += " (Entrée pour garder l'actuelle)"
        url = input(f"{invite} : ").strip() or defaut
        if not validate_webhook_url(url):
            print("❌ URL invalide. Attendu : https://discord.com/api/webhooks/<id>/<token>")
            continue
        print("🔔 Envoi d'un message de test…")
        if tester_webhook(url):
            print("✅ Le webhook répond, un message de test est arrivé dans votre salon.")
            config["webhook"] = url
            break
        print("❌ Le message de test n'est pas passé (détail sur la ligne au-dessus).")
        print("   Si l'URL est bonne : vérifiez la connexion, un VPN ou un pare-feu, et que le programme est à jour.")
        if _demander("Réessayer avec une autre URL ? (o/n)", "o").lower().startswith("n"):
            raise SystemExit(1)

    print()
    print("Réglages (Entrée pour garder la valeur proposée) :")
    while True:
        charset = _demander("Jeu de caractères : letters (a-z), alnum (a-z 0-9), full (a-z 0-9 _ .)", str(config["charset"])).lower()
        if charset in ("letters", "alnum", "full"):
            config["charset"] = charset
            break
        print("❌ Répondez letters, alnum ou full.")
    while True:
        try:
            rps = float(_demander("Requêtes par seconde (2 est prudent, montez seulement sans 429)", str(config["rps"])))
            if rps <= 0:
                raise ValueError
            config["rps"] = rps
            break
        except ValueError:
            print("❌ Entrez un nombre supérieur à 0.")

    enregistrer_config(config)
    print()
    print(f"✅ Configuration enregistrée dans {CONFIG_PATH}")
    print("   Modifiez ce fichier pour changer les réglages, ou relancez avec --reconfigurer.")
    print()
    return config


def tester_webhook(url: str) -> bool:
    from discord_username_checker.webhook import DiscordWebhook

    try:
        hook = DiscordWebhook(url)
    except ValueError:
        return False
    return hook.send("🔔 Test : le vérificateur de pseudos Discord est bien connecté à ce webhook.")


def notifier(config: Dict[str, object], texte: str) -> None:
    """Envoie un message au webhook sans jamais faire échouer le lanceur."""
    from discord_username_checker.webhook import DiscordWebhook

    try:
        DiscordWebhook(str(config.get("webhook") or ""), max_retries=1).send(texte)
    except Exception:  # noqa: BLE001 - une notification ratée ne doit rien casser
        pass


# ---------------------------------------------------------------------------
# Exécution du vérificateur
# ---------------------------------------------------------------------------

def compter_pseudos(path: Path) -> int:
    """Nombre de pseudos actifs (lignes non vides, hors commentaires) dans la liste."""
    try:
        lignes = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for l in lignes if l.strip() and not l.strip().startswith("#"))


def mode_surveillance(config: Dict[str, object]) -> bool:
    return str(config.get("mode") or "surveillance").strip().lower() != "massif"


def construire_commande(config: Dict[str, object], extra: Optional[List[str]] = None) -> List[str]:
    commande = [
        sys.executable,
        "-m",
        "discord_username_checker",
        "--webhook", str(config["webhook"]),
        "--rps", str(config["rps"]),
        "--webhook-interval", str(config["webhook_interval"]),
        "--output-dir", str(config["output_dir"]),
    ]
    if mode_surveillance(config):
        commande += [
            "--surveiller",
            "--wordlist", str(config.get("pseudos") or "pseudos.txt"),
            "--recheck-hours", str(config.get("recheck_heures", 24)),
            "--workers", "1",
            "--no-summary",
        ]
        if extra:
            commande += list(extra)
        return commande
    commande += [
        "--charset", str(config["charset"]),
        "--length", str(int(config["length"])),
        "--workers", str(int(config["workers"])),
        "--webhook-batch", str(int(config["webhook_batch"])),
    ]
    pattern = str(config.get("pattern") or "").strip()
    if pattern:
        commande += ["--pattern", pattern]
    limit = int(config.get("limit") or 0)
    if limit > 0:
        commande += ["--limit", str(limit)]
    if extra:
        commande += list(extra)
    return commande


def executer(commande: List[str], journal: Path) -> Tuple[int, bool]:
    """Lance le vérificateur, affiche et journalise sa sortie.

    Retourne ``(code de sortie, interrompu par Ctrl+C)``.
    """
    journal.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    interrompu = False
    # L'enfant est placé dans son propre groupe : le Ctrl+C du terminal n'atteint que le
    # lanceur, qui transmet exactement un signal d'arrêt à l'enfant (voir interrompre_enfant).
    options = {}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    with open(journal, "a", encoding="utf-8") as log:
        log.write(f"\n===== Lancement {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        process = subprocess.Popen(
            commande,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **options,
        )
        assert process.stdout is not None

        def recopier() -> None:
            for ligne in process.stdout:
                sys.stdout.write(ligne)
                sys.stdout.flush()
                log.write(ligne)

        try:
            recopier()
        except KeyboardInterrupt:
            interrompu = True
            print("\n⏹️  Arrêt demandé : patientez, la vérification termine proprement… (Ctrl+C encore pour forcer)")
            interrompre_enfant(process)
            try:
                recopier()
            except KeyboardInterrupt:
                process.kill()
        code = process.wait()
        log.write(f"===== Fin (code {code}{', interrompu' if interrompu else ''}) =====\n")
    return code, interrompu


def interrompre_enfant(process: "subprocess.Popen[str]") -> None:
    """Demande un arrêt propre au vérificateur (un seul signal, jamais deux)."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)  # reçu comme SIGBREAK par l'enfant
        else:
            process.send_signal(signal.SIGINT)
    except (OSError, ValueError):
        process.terminate()


def decider(
    code: int,
    interrompu: bool,
    relances_blocage: int,
    relances_erreur: int,
    config: Dict[str, object],
) -> Tuple[str, float, str]:
    """Décide quoi faire après un passage : (action, attente en secondes, explication)."""
    if interrompu:
        return ACTION_ARRET, 0.0, "arrêt demandé par l'utilisateur"
    if code == 0:
        return ACTION_TERMINE, 0.0, "tous les pseudos prévus ont été vérifiés"
    if code == EXIT_FATAL_CHECKER:
        if relances_blocage >= int(config["relances_max_blocage"]):
            return ACTION_ARRET, 0.0, "Discord bloque toujours après plusieurs tentatives"
        minutes = float(config["pause_blocage_minutes"])
        return ACTION_RELANCE, minutes * 60.0, f"Discord a bloqué la vérification, nouvelle tentative dans {minutes:g} min"
    if relances_erreur >= int(config["relances_max_erreur"]):
        return ACTION_ARRET, 0.0, "trop d'erreurs consécutives, consultez resultats/journal.log"
    secondes = float(config["pause_erreur_secondes"])
    return ACTION_RELANCE, secondes, f"le vérificateur s'est arrêté avec une erreur, nouvelle tentative dans {secondes:g} s"


def attendre(secondes: float) -> bool:
    """Attente interruptible par Ctrl+C. Retourne False si interrompue."""
    fin = time.monotonic() + secondes
    try:
        while True:
            restant = fin - time.monotonic()
            if restant <= 0:
                return True
            minutes, sec = divmod(int(restant), 60)
            sys.stdout.write(f"\r⏳ Reprise dans {minutes:02d}:{sec:02d}  (Ctrl+C pour quitter) ")
            sys.stdout.flush()
            time.sleep(min(1.0, restant))
    except KeyboardInterrupt:
        print()
        return False


# ---------------------------------------------------------------------------
# Mise à jour
# ---------------------------------------------------------------------------

def telecharger(url: str, timeout: float = 120.0) -> bytes:
    requete = urllib.request.Request(url, headers={"User-Agent": "lancer.py (mise a jour)"})
    with urllib.request.urlopen(requete, timeout=timeout) as reponse:
        return reponse.read()


def installer_zip(donnees: bytes, destination: Path) -> int:
    """Décompresse l'archive GitHub par-dessus ``destination``. Retourne le nombre de fichiers écrits."""
    ecrits = 0
    with zipfile.ZipFile(io.BytesIO(donnees)) as archive:
        entrees = [e for e in archive.infolist() if not e.is_dir()]
        if not entrees:
            raise ValueError("archive vide")
        racine = entrees[0].filename.split("/")[0]  # dossier ajouté par GitHub (Test-<branche>/)
        for entree in entrees:
            morceaux = entree.filename.split("/")
            if morceaux[0] != racine or len(morceaux) < 2:
                continue
            relatif = morceaux[1:]
            if relatif[0] in FICHIERS_PRESERVES or ".." in relatif:
                continue
            cible = destination.joinpath(*relatif)
            cible.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entree) as source, open(cible, "wb") as fichier:
                shutil.copyfileobj(source, fichier)
            ecrits += 1
    return ecrits


def mettre_a_jour() -> int:
    """Met le programme à jour : ``git pull`` si possible, sinon téléchargement de l'archive."""
    if (ROOT / ".git").exists() and shutil.which("git"):
        print("⬇️  Mise à jour avec git pull…")
        resultat = subprocess.run(["git", "pull", "--ff-only"], cwd=str(ROOT))
        if resultat.returncode == 0:
            print("✅ Programme à jour. Relancez Lancer.bat (ou ./lancer.sh).")
            return 0
        print("⚠️  git pull a échoué, passage au téléchargement de l'archive.")
    print("⬇️  Téléchargement de la dernière version…")
    try:
        donnees = telecharger(UPDATE_ZIP_URL)
        ecrits = installer_zip(donnees, ROOT)
    except Exception as error:  # noqa: BLE001 - tout échec doit être expliqué à l'utilisateur
        print(f"❌ Mise à jour impossible : {error}")
        print(f"   Téléchargez l'archive à la main : {UPDATE_ZIP_URL}")
        return 1
    print(f"✅ {ecrits} fichier(s) mis à jour (config.json et resultats/ conservés).")
    print("   Relancez Lancer.bat (ou ./lancer.sh).")
    return 0


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def verifier_environnement() -> bool:
    if sys.version_info < (3, 9):
        print(f"❌ Python 3.9 ou plus récent est nécessaire (vous avez {sys.version.split()[0]}).")
        print("   Téléchargez-le sur https://www.python.org/downloads/")
        return False
    if not (ROOT / "discord_username_checker" / "cli.py").exists():
        print("❌ Le dossier discord_username_checker est introuvable à côté de lancer.py.")
        return False
    return True


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(
        description="Lance le vérificateur de pseudos Discord avec configuration guidée et relance automatique.",
        epilog="Toute option inconnue est transmise telle quelle au vérificateur (ex. --pattern 'a??z').",
    )
    parser.add_argument("--reconfigurer", action="store_true", help="relancer l'assistant de configuration")
    parser.add_argument("--une-fois", action="store_true", help="un seul passage, sans relance automatique")
    parser.add_argument("--mettre-a-jour", action="store_true", help="télécharger la dernière version puis quitter")
    args, extra = parser.parse_known_args(argv)

    if args.mettre_a_jour:
        return mettre_a_jour()

    if not verifier_environnement():
        return 1
    sys.path.insert(0, str(ROOT))

    config = charger_config()
    if config is None or args.reconfigurer or not str(config.get("webhook") or "").strip():
        try:
            config = assistant_configuration(config)
        except (KeyboardInterrupt, EOFError):
            print("\nConfiguration annulée.")
            return 1

    if mode_surveillance(config):
        pseudos = ROOT / str(config.get("pseudos") or "pseudos.txt")
        if not pseudos.exists():
            pseudos.write_text(MODELE_PSEUDOS, encoding="utf-8")
        nombre = compter_pseudos(pseudos)
        if nombre == 0:
            print("📝 Aucun pseudo à surveiller pour l'instant.")
            print(f"   Ouvrez le fichier {pseudos} avec le Bloc-notes, écrivez les pseudos qui vous")
            print("   intéressent (un par ligne), enregistrez, puis relancez.")
            print("   Discord n'autorise qu'environ 5 vérifications par heure : une liste courte suffit.")
            return 1
        print(f"👀 Mode surveillance : {nombre} pseudo(s) dans {pseudos.name}, re-vérifiés toutes les "
              f"{config.get('recheck_heures', 24)} h.")
    else:
        print("⚠️  Mode massif : Discord n'autorise qu'environ 5 vérifications par heure, un balayage complet")
        print("   prendra des années. Passez mode à \"surveillance\" dans config.json pour une liste ciblée.")

    journal = ROOT / str(config["output_dir"]) / "journal.log"
    print(f"📓 Journal : {journal}")
    print("▶️  Démarrage (Ctrl+C pour arrêter, la reprise est automatique).")
    print()

    relances_blocage = 0
    relances_erreur = 0
    while True:
        debut = time.monotonic()
        code, interrompu = executer(construire_commande(config, extra), journal)
        duree = time.monotonic() - debut
        if duree > 600:  # un passage qui a duré : les compteurs repartent de zéro
            relances_blocage = 0
            relances_erreur = 0

        action, attente_s, explication = decider(code, interrompu, relances_blocage, relances_erreur, config)
        print()
        if action == ACTION_TERMINE:
            print(f"🏁 Terminé : {explication}.")
            print("   Les pseudos disponibles sont dans", ROOT / str(config["output_dir"]) / "disponibles.txt")
            return 0
        if action == ACTION_ARRET:
            print(f"⏹️  Arrêt : {explication}.")
            if not interrompu:
                notifier(config, f"⛔ Le lanceur s'est arrêté : {explication}.")
                return 1
            return 0
        if args.une_fois:
            print(f"⏹️  {explication}. Pas de relance (--une-fois).")
            return 1

        if code == EXIT_FATAL_CHECKER:
            relances_blocage += 1
        else:
            relances_erreur += 1
        print(f"🔁 {explication}.")
        notifier(config, f"⏸️ {explication}.")
        if not attendre(attente_s):
            print("⏹️  Arrêt demandé.")
            return 0
        print()


if __name__ == "__main__":
    sys.exit(main())

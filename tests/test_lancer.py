import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import lancer

URL = "https://discord.com/api/webhooks/123456789012345678/token-abc"


def config_test(**overrides):
    config = dict(lancer.DEFAULT_CONFIG)
    config["webhook"] = URL
    config.update(overrides)
    return config


class DeciderTests(unittest.TestCase):
    def test_interruption_stops(self):
        action, delay, _ = lancer.decider(0, True, 0, 0, config_test())
        self.assertEqual(action, lancer.ACTION_ARRET)
        self.assertEqual(delay, 0)

    def test_success_is_done(self):
        action, _, _ = lancer.decider(0, False, 3, 3, config_test())
        self.assertEqual(action, lancer.ACTION_TERMINE)

    def test_block_retries_with_long_pause(self):
        action, delay, message = lancer.decider(2, False, 0, 0, config_test(pause_blocage_minutes=15))
        self.assertEqual(action, lancer.ACTION_RELANCE)
        self.assertEqual(delay, 900)
        self.assertIn("bloqué", message)

    def test_block_gives_up_after_max(self):
        action, _, _ = lancer.decider(2, False, 10, 0, config_test(relances_max_blocage=10))
        self.assertEqual(action, lancer.ACTION_ARRET)

    def test_error_retries_then_gives_up(self):
        action, delay, _ = lancer.decider(1, False, 0, 4, config_test(pause_erreur_secondes=7, relances_max_erreur=5))
        self.assertEqual(action, lancer.ACTION_RELANCE)
        self.assertEqual(delay, 7)
        action, _, _ = lancer.decider(1, False, 0, 5, config_test(relances_max_erreur=5))
        self.assertEqual(action, lancer.ACTION_ARRET)


class CommandeTests(unittest.TestCase):
    def test_surveillance_is_default(self):
        commande = lancer.construire_commande(config_test(recheck_heures=12), ["--cycles", "1"])
        self.assertIn("--surveiller", commande)
        self.assertEqual(commande[commande.index("--wordlist") + 1], "pseudos.txt")
        self.assertEqual(commande[commande.index("--recheck-hours") + 1], "12")
        self.assertEqual(commande[commande.index("--workers") + 1], "1")
        self.assertNotIn("--charset", commande)
        self.assertEqual(commande[-2:], ["--cycles", "1"])

    def test_compter_pseudos_and_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pseudos.txt"
            path.write_text(lancer.MODELE_PSEUDOS, encoding="utf-8")
            self.assertEqual(lancer.compter_pseudos(path), 0)  # le modèle ne contient que des commentaires
            path.write_text("# titre\nnova\n\nzed_\n", encoding="utf-8")
            self.assertEqual(lancer.compter_pseudos(path), 2)
            self.assertEqual(lancer.compter_pseudos(Path(tmp) / "absent.txt"), 0)

    def test_base_command(self):
        commande = lancer.construire_commande(config_test(mode="massif"))
        self.assertEqual(commande[:3], [sys.executable, "-m", "discord_username_checker"])
        self.assertIn("--webhook", commande)
        self.assertEqual(commande[commande.index("--charset") + 1], "letters")
        self.assertNotIn("--pattern", commande)
        self.assertNotIn("--limit", commande)

    def test_pattern_limit_and_extra(self):
        commande = lancer.construire_commande(
            config_test(mode="massif", pattern="a??z", limit=50), ["--endpoint", "http://x"]
        )
        self.assertEqual(commande[commande.index("--pattern") + 1], "a??z")
        self.assertEqual(commande[commande.index("--limit") + 1], "50")
        self.assertEqual(commande[-2:], ["--endpoint", "http://x"])


class ConfigTests(unittest.TestCase):
    def test_round_trip_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            self.assertIsNone(lancer.charger_config(path))
            lancer.enregistrer_config({"webhook": URL, "rps": 5}, path)
            config = lancer.charger_config(path)
            self.assertEqual(config["webhook"], URL)
            self.assertEqual(config["rps"], 5)
            self.assertEqual(config["charset"], "letters")  # valeur par défaut complétée

    def test_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{pas du json", encoding="utf-8")
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertIsNone(lancer.charger_config(path))


class AssistantTests(unittest.TestCase):
    def test_wizard_validates_url_and_saves(self):
        answers = iter(["https://example.com/pas-un-webhook", URL, "alnum", "3"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            lancer, "CONFIG_PATH", Path(tmp) / "config.json"
        ), mock.patch.object(lancer, "tester_webhook", return_value=True) as tester, mock.patch(
            "builtins.input", lambda _prompt: next(answers)
        ), mock.patch("sys.stdout", new_callable=io.StringIO):
            config = lancer.assistant_configuration()
            saved = json.loads((Path(tmp) / "config.json").read_text(encoding="utf-8"))
        tester.assert_called_once_with(URL)
        self.assertEqual(config["webhook"], URL)
        self.assertEqual(config["charset"], "alnum")
        self.assertEqual(config["rps"], 3.0)
        self.assertEqual(saved["webhook"], URL)

    def test_wizard_keeps_defaults_on_enter(self):
        answers = iter([URL, "", ""])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            lancer, "CONFIG_PATH", Path(tmp) / "config.json"
        ), mock.patch.object(lancer, "tester_webhook", return_value=True), mock.patch(
            "builtins.input", lambda _prompt: next(answers)
        ), mock.patch("sys.stdout", new_callable=io.StringIO):
            config = lancer.assistant_configuration()
        self.assertEqual(config["charset"], "letters")
        self.assertEqual(config["rps"], 2.0)


class ExecuterTests(unittest.TestCase):
    def test_output_is_shown_and_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "sous" / "journal.log"
            commande = [sys.executable, "-c", "print('bonjour'); import sys; sys.exit(3)"]
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code, interrompu = lancer.executer(commande, journal)
            self.assertEqual(code, 3)
            self.assertFalse(interrompu)
            self.assertIn("bonjour", out.getvalue())
            contenu = journal.read_text(encoding="utf-8")
            self.assertIn("bonjour", contenu)
            self.assertIn("code 3", contenu)


if __name__ == "__main__":
    unittest.main()


class MiseAJourTests(unittest.TestCase):
    @staticmethod
    def archive(fichiers):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            for nom, contenu in fichiers.items():
                z.writestr("Test-branche/" + nom, contenu)
        return buffer.getvalue()

    def test_installe_le_code_et_preserve_les_donnees(self):
        donnees = self.archive({
            "lancer.py": "nouveau",
            "discord_username_checker/cli.py": "cli",
            "config.json": "PAS TOUCHE",
            "resultats/disponibles.txt": "PAS TOUCHE",
        })
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / "config.json").write_text("ma config", encoding="utf-8")
            (dest / "resultats").mkdir()
            (dest / "resultats" / "disponibles.txt").write_text("abcd\n", encoding="utf-8")
            ecrits = lancer.installer_zip(donnees, dest)
            self.assertEqual(ecrits, 2)
            self.assertEqual((dest / "lancer.py").read_text(encoding="utf-8"), "nouveau")
            self.assertEqual((dest / "discord_username_checker" / "cli.py").read_text(encoding="utf-8"), "cli")
            self.assertEqual((dest / "config.json").read_text(encoding="utf-8"), "ma config")
            self.assertEqual((dest / "resultats" / "disponibles.txt").read_text(encoding="utf-8"), "abcd\n")

    def test_archive_vide_refusee(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w"):
            pass
        with self.assertRaises(ValueError):
            lancer.installer_zip(buffer.getvalue(), Path("."))

    def test_option_mettre_a_jour(self):
        with mock.patch.object(lancer, "mettre_a_jour", return_value=0) as maj:
            self.assertEqual(lancer.main(["--mettre-a-jour"]), 0)
        maj.assert_called_once()

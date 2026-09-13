# Discord Username Checker

Outil en ligne de commande qui surveille la disponibilité de pseudos Discord et vous
prévient sur un **webhook Discord** dès que l'un d'eux se libère. Il sait aussi balayer
des combinaisons en masse, mais Discord limite fortement cet usage (voir plus bas).

- **Aucune dépendance** : Python 3.9+ et sa bibliothèque standard, rien à installer.
- **Aucun token de compte** : l'outil utilise l'endpoint public que la page d'inscription
  de Discord appelle elle-même (`unique-username/username-attempt-unauthed`).
- **Respect des limites** : débit configurable, pause automatique sur `429` avec le délai
  demandé par Discord, arrêt net si Discord exige un captcha ou bloque l'IP.
- **Reprise** : les résultats sont écrits au fur et à mesure ; si vous coupez (`Ctrl+C`)
  puis relancez, les pseudos déjà vérifiés sont ignorés.
- **Webhook** : les pseudos disponibles partent par lots (dès 25 trouvés ou toutes les
  60 s), plus un récapitulatif à la fin.

## Lancement automatique (le plus simple)

1. Installez Python 3 (python.org ; sur Windows cochez « Add Python to PATH »).
2. Récupérez le dossier du projet (`git clone` ou « Code → Download ZIP » sur GitHub).
3. Lancez :
   - **Windows** : double-cliquez sur `Lancer.bat`
   - **Mac / Linux** : `./lancer.sh` dans un terminal
   - ou, partout : `python lancer.py`

Au premier lancement, un assistant demande l'URL de votre webhook Discord, envoie un
message de test dans votre salon et enregistre le tout dans `config.json`. Un fichier
`pseudos.txt` est créé à côté : ouvrez-le avec le Bloc-notes, écrivez les pseudos qui vous
intéressent (un par ligne) et relancez. La surveillance démarre alors toute seule et :

- vérifie chaque pseudo de la liste au rythme autorisé par Discord (environ 5 par heure
  et par adresse IP), puis les re-vérifie toutes les 24 h (`recheck_heures` dans
  `config.json`) ;
- vous prévient sur le webhook à l'instant où un pseudo de la liste devient disponible
  (une seule fois, tant qu'il reste libre) ;

- reprend là où elle s'était arrêtée à chaque relance ;
- redémarre d'elle-même après une erreur ou un plantage (pause de 60 s), et après un
  blocage de Discord comme un captcha ou un 403 (pause de 30 min), en vous prévenant
  sur le webhook ;
- s'arrête proprement avec Ctrl+C, et une fois tous les pseudos vérifiés ;
- écrit tout ce qui s'affiche dans `resultats/journal.log`.

Pour changer les réglages, modifiez `config.json` (jeu de caractères, vitesse, motif,
temps de pause, nombre de relances) ou relancez avec `python lancer.py --reconfigurer`.
Toute option du vérificateur peut être ajoutée à la suite, par exemple
`python lancer.py --pattern 'a??z'`. Ne partagez pas `config.json` : il contient le
token du webhook (il est ignoré par Git).

### Mettre à jour

- **Windows** : double-cliquez sur `MettreAJour.bat`.
- **Mac / Linux** : `./lancer.sh --mettre-a-jour`.

La dernière version est téléchargée et installée par-dessus l'ancienne ; `config.json` et
le dossier `resultats/` sont conservés. Relancez ensuite normalement.

### Démarrer avec l'ordinateur (facultatif)

- **Windows** : Planificateur de tâches → Créer une tâche de base → déclencheur
  « À l'ouverture de session » → action « Démarrer un programme » → parcourir jusqu'à
  `Lancer.bat`, et renseignez le dossier du projet dans « Commencer dans ».
- **Mac / Linux** : `crontab -e` puis ajoutez
  `@reboot cd /chemin/vers/Test && ./lancer.sh >> resultats/cron.log 2>&1`.

## Installation manuelle

```bash
git clone https://github.com/nicocebanita-cloud/Test.git
cd Test
python -m discord_username_checker --help
```

Facultatif, pour avoir la commande `discord-username-checker` partout :

```bash
pip install -e .
```

## Utilisation rapide

```bash
# 1. Créez un webhook dans Discord : Paramètres du salon → Intégrations → Webhooks.
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/<id>/<token>"

# 2. Vérifiez que le webhook répond.
python -m discord_username_checker --test-webhook

# 3. Vérifiez quelques pseudos précis.
python -m discord_username_checker abcd wxyz qsdf

# 4. Vérification massive : toutes les combinaisons de 4 lettres (456 976 pseudos).
python -m discord_username_checker --charset letters --rps 2 --workers 2
```

Sur Windows, remplacez `export ...` par `set DISCORD_WEBHOOK_URL=...` (ou passez
`--webhook <url>` sur la ligne de commande).

## Pourquoi une liste plutôt qu'un balayage

Discord n'autorise qu'une poignée de vérifications par adresse IP, puis répond 429
« Le nombre d'actions de la ressource est limité » avec une attente d'environ 30 minutes.
Cela fait à peu près 5 pseudos par heure, soit 120 par jour : les 456 976 combinaisons
de 4 lettres demanderaient une dizaine d'années. Le mode surveillance tire le meilleur
parti de ce quota : une liste courte de pseudos que vous voulez vraiment, parcourue en
quelques heures puis re-vérifiée chaque jour, avec une alerte dès qu'un pseudo se libère
(un compte supprimé rend son pseudo au bout d'un certain temps).

En ligne de commande, la surveillance s'active avec `--surveiller`, sur les pseudos donnés
en argument ou dans `--wordlist`, avec `--recheck-hours` (défaut 24) et `--cycles` pour
limiter le nombre de passages (défaut : sans fin).

Le mode massif reste disponible (`"mode": "massif"` dans `config.json`, ou les options
ci-dessous sans `--surveiller`), pour de petites cibles comme un motif `--pattern`.

## Choisir quoi tester

| Option | Effet |
| --- | --- |
| `--length 4` | longueur des pseudos générés (2 à 32) |
| `--charset letters` | `letters` (a-z), `alnum` (a-z + 0-9), `full` (a-z, 0-9, `_`, `.`) ou une liste de caractères, ex. `--charset abcxyz` |
| `--pattern 'a??z'` | motif avec `?` comme joker : ici tous les pseudos qui commencent par `a` et finissent par `z` |
| `--wordlist mots.txt` | pseudos d'un fichier, un par ligne (`#` pour commenter) |
| `--shuffle [--seed N]` | ordre aléatoire (reproductible avec une graine) |
| `--limit N` | s'arrêter après N pseudos |
| `--dry-run` | afficher ce qui serait testé et la durée estimée, sans appeler Discord |

Les pseudos impossibles selon les règles Discord (majuscules, `..`, mots réservés comme
`here`) sont filtrés avant tout appel réseau.

Ordre de grandeur : 4 lettres = 456 976 pseudos, soit environ 2,6 jours à 2 requêtes/s
et 13 h à 10 requêtes/s. Utilisez `--dry-run` pour voir l'estimation exacte.

## Vitesse et sécurité

| Option | Défaut | Rôle |
| --- | --- | --- |
| `--rps` | 2 | requêtes par seconde, tous threads confondus |
| `--workers` | 2 | threads simultanés |
| `--timeout` | 15 | délai HTTP en secondes |
| `--max-retries` | 3 | nouveaux essais sur erreur réseau ou 5xx |
| `--abort-after-problems` | 20 | arrêt après N erreurs consécutives (0 = jamais) |
| `--proxy` | | proxy HTTP(S), ex. `http://127.0.0.1:8080` |
| `--header 'Nom: valeur'` | | en-tête HTTP supplémentaire (répétable) |

Ce que fait l'outil quand Discord réagit :

- **429 (limite de débit)** : tous les threads se mettent en pause le temps indiqué par
  Discord, puis reprennent. Le pseudo en cours est retenté, pas perdu.
- **Captcha demandé** ou **403** : arrêt immédiat avec un message clair. Continuer serait
  inutile ; baissez `--rps`, attendez, ou changez d'adresse IP.
- **Erreurs réseau ou 5xx** : nouveaux essais avec délai croissant, puis le pseudo est
  marqué `error` et sera retenté à la prochaine reprise.
- **Réponse inattendue** : marquée `unknown`, avec le corps de la réponse dans le CSV.
  Si cela se répète, l'API a probablement changé : l'outil s'arrête tout seul.

**Limite constatée en pratique.** Environ 5 requêtes par adresse IP, puis un 429 avec une
attente d'environ 30 minutes. L'outil respecte cette attente intégralement, l'affiche avec
l'heure de reprise, et la mémorise dans `resultats/pause.json` : si vous relancez avant
l'heure, il patiente au lieu de réessayer et d'allonger le blocage. L'option `--rps` ne
sert donc qu'à espacer les requêtes ; elle ne permet pas de dépasser ce quota.

## Fichiers produits

Tout va dans le dossier `resultats/` (modifiable avec `--output-dir`) :

- `disponibles.txt` : un pseudo disponible par ligne. C'est la référence, même si le
  webhook tombe en panne.
- `resultats.csv` : `username,status,http_status,detail,checked_at` pour chaque pseudo
  testé. Les statuts possibles : `available`, `taken`, `invalid`, `error`, `unknown`.
- `pause.json` : heure de reprise imposée par le dernier 429, respectée au relancement.

À la relance, les pseudos avec un statut définitif (`available`, `taken`, `invalid`) sont
ignorés ; `--no-resume` force une nouvelle vérification complète.

## Webhook

| Option | Défaut | Rôle |
| --- | --- | --- |
| `--webhook` | `$DISCORD_WEBHOOK_URL` | URL du webhook |
| `--webhook-name` | Vérificateur de pseudos | nom affiché (Discord refuse les mots « discord » et « clyde ») |
| `--webhook-batch` | 25 | envoyer dès N pseudos disponibles |
| `--webhook-interval` | 60 | envoyer au plus tard toutes les N secondes |
| `--no-summary` | | ne pas envoyer le récapitulatif final |
| `--test-webhook` | | envoyer un message de test puis quitter |

Les messages font moins de 2000 caractères, ne déclenchent aucune mention (`@everyone`
compris) et respectent la limite de débit des webhooks. Ne partagez jamais l'URL du
webhook : elle permet à quiconque de poster dans votre salon.

## Tests

```bash
python -m unittest -v
```

Les tests simulent toutes les réponses de Discord (200, 400, 429, captcha, 403, 5xx,
coupures réseau) et exercent la couche HTTP contre un serveur local. Ils tournent aussi
dans GitHub Actions à chaque push.

## Limites connues

- L'endpoint n'est pas documenté officiellement par Discord ; s'il change, l'outil le
  signale (`unknown`) et s'arrête. `--endpoint` permet d'en indiquer un autre.
- Un pseudo « disponible » à l'instant T peut être pris entre-temps.

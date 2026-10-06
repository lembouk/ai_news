# Brief IA quotidien → Discord

Chaque matin, un message Discord avec les nouveautés IA, classées par thème
(modèles, open source, actu tech, économie/régulation, cas d'usage).
Aucune clé API : seulement le webhook Discord.

## Installation (5 min)
1. **Discord** : crée un serveur → un salon `#brief-ia` → Paramètres du salon → Intégrations → Webhooks → Nouveau webhook → *Copier l'URL*.
2. **GitHub** : crée un dépôt (ex. `ia-tracker`) et pousse ces fichiers à la racine.
3. Dépôt → Settings → Secrets and variables → Actions → *New repository secret* :
   `DISCORD_WEBHOOK_URL` = l'URL copiée.
4. Onglet Actions → « Brief IA quotidien » → *Run workflow* pour tester.
5. **Téléphone** : installe l'app Discord, active les notifications du salon
   (clic long sur le salon → Notifications → Tous les messages). Les bannières viennent de là.

## Personnaliser
- Sujets / sources : dictionnaire `SOURCES` en haut de `ia_tracker.py`
  (RSS, Hacker News, Hugging Face trending, Google News par requête).
- Heure : `cron` dans `.github/workflows/ia-brief.yml` (en UTC).
- Volume : `PER_CATEGORY` et `MAX_AGE_H`.
- Test local sans envoyer : `python ia_tracker.py --dry-run`

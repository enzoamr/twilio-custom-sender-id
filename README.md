# Twilio SMS Panel

Panel interactif (terminal) pour envoyer des SMS via l'**API officielle Twilio**,
avec choix du *sender ID* (numéro Twilio ou identifiant alphanumérique
personnalisé), du message et des destinataires.

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Copiez `.env.example` en `.env` et renseignez vos identifiants Twilio
(disponibles sur https://console.twilio.com) :

```bash
cp .env.example .env      # Windows : copy .env.example .env
```

Deux méthodes d'authentification possibles (choisissez-en une) :

**Méthode 1 — API Key (recommandée, révocable) :**
```dotenv
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxx
TWILIO_API_KEY_SID=SKxxxxxxxxxxxxxxxxxxxx
TWILIO_API_KEY_SECRET=xxxxxxxxxxxxxxxxxxxx
```

**Méthode 2 — Auth Token (classique) :**
```dotenv
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
```

> Si le `.env` est absent, le panel propose un assistant qui le crée pour vous.
> Le fichier `.env` est ignoré par git (`.gitignore`).

## Utilisation

**Mode panel interactif :**

```bash
python twilio_sms_panel.py
```

Menu : envoyer un SMS, lister vos numéros Twilio, vérifier le statut d'un
message, infos du compte.

**Envoi direct (CLI) :**

```bash
python twilio_sms_panel.py --to "+33612345678" --from "MonService" --body "Bonjour !"
```

**Simulation (sans envoi réel) :**

```bash
python twilio_sms_panel.py --to "+33612345678" --from "MonService" --body "Test" --dry-run
```

## Fonctionnalités

- ✅ Vérification du compte + solde au démarrage
- ✅ Sender ID : numéro Twilio **ou** alpha sender personnalisé (≤ 11 car.)
- ✅ Validation des numéros (E.164) + infos pays/opérateur
- ✅ Compteur de segments SMS (GSM-7 / Unicode) en direct
- ✅ Envoi mono ou multi-destinataires + récapitulatif
- ✅ Tableau de résultats + aide sur les codes d'erreur Twilio
- ✅ Suivi du statut d'un message

## ⚠️ Usage responsable

- N'utilisez que des sender IDs que vous êtes **autorisé** à utiliser.
- Dans de nombreux pays (France, US, Canada…), Twilio exige
  l'**enregistrement préalable** de l'alpha sender ; sans cela le message est
  rejeté (codes `21266`, `30450`…). C'est ce mécanisme qui empêche
  l'usurpation de marques.
- Un alpha sender est **one-way** : le destinataire ne peut pas répondre.
- Se faire passer pour autrui (banque, administration…) viole les CGU Twilio
  et est illégal dans la plupart des juridictions.
- Comptes *trial* : envoi limité aux numéros vérifiés, préfixe ajouté au message.

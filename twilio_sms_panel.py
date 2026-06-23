#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Twilio SMS Panel
================
Panel interactif pour envoyer des SMS via l'API **officielle Twilio**, avec
choix du *sender ID* (numéro Twilio ou identifiant alphanumérique personnalisé),
du message et des destinataires.

Fonctionnalités
---------------
- Configuration guidée des identifiants (écrit un fichier .env, jamais en dur)
- Vérification du compte au démarrage (nom, statut trial/complet, solde)
- Choix du sender ID : numéro Twilio du compte OU alpha sender personnalisé
- Validation des numéros (E.164) via `phonenumbers` + infos pays
- Compteur de segments SMS en direct (GSM-7 / Unicode UCS-2)
- Envoi mono ou multi-destinataires, avec récapitulatif et confirmation
- Tableau de résultats (SID, statut, erreur éventuelle) + aide sur les codes
- Suivi du statut d'un message déjà envoyé
- Mode CLI non-interactif (--to / --from / --body) + --dry-run

⚠️  Usage responsable
---------------------
N'utilisez QUE des sender IDs que vous êtes autorisé à utiliser. Dans de
nombreux pays Twilio exige l'enregistrement préalable de l'alpha sender ;
sans enregistrement le message sera rejeté (codes 21266/30450/...). Se faire
passer pour autrui (banque, administration...) est interdit par les CGU
Twilio et illégal dans la plupart des juridictions.

Installation
------------
    pip install -r requirements.txt
    # ou : pip install twilio rich python-dotenv phonenumbers

Lancement
---------
    python twilio_sms_panel.py                 # mode panel interactif
    python twilio_sms_panel.py --to +336... \\
        --from "MonService" --body "Bonjour"   # envoi direct
    python twilio_sms_panel.py --to +336... --from "MonService" \\
        --body "Test" --dry-run                # simulation sans envoi
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Dépendances tierces — message clair si elles manquent
# ---------------------------------------------------------------------------
try:
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException, TwilioException
except ImportError:  # pragma: no cover
    sys.exit(
        "❌  Dépendance manquante : 'twilio'.\n"
        "    Installez les dépendances avec :  pip install -r requirements.txt"
    )

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt, Confirm, IntPrompt
    from rich.text import Text
    from rich import box
except ImportError:  # pragma: no cover
    sys.exit(
        "❌  Dépendance manquante : 'rich'.\n"
        "    Installez les dépendances avec :  pip install -r requirements.txt"
    )

try:
    from dotenv import load_dotenv, set_key
except ImportError:  # pragma: no cover
    sys.exit(
        "❌  Dépendance manquante : 'python-dotenv'.\n"
        "    Installez les dépendances avec :  pip install -r requirements.txt"
    )

# phonenumbers est optionnel : on dégrade proprement si absent.
try:
    import phonenumbers
    from phonenumbers import geocoder, carrier, NumberParseException

    _HAS_PHONENUMBERS = True
except ImportError:  # pragma: no cover
    _HAS_PHONENUMBERS = False


console = Console()

# Chemin du fichier .env (à côté du script)
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# ---------------------------------------------------------------------------
# Jeu de caractères GSM 03.38 (pour le calcul des segments SMS)
# ---------------------------------------------------------------------------
GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# Ces caractères occupent 2 « septets » en GSM-7
GSM7_EXTENDED = "^{}\\[~]|€"

# Limites de longueur des sender IDs alphanumériques (norme Twilio)
ALPHA_MAX_LEN = 11

# Aide contextuelle pour les codes d'erreur Twilio les plus fréquents.
TWILIO_ERROR_HINTS = {
    21211: "Numéro destinataire invalide. Vérifiez le format E.164 (+336...).",
    21214: "Numéro destinataire injoignable / invalide selon Twilio.",
    21408: "Permission d'envoi vers cette zone géographique désactivée. "
           "Activez la 'Geo Permission' du pays dans la console Twilio.",
    21606: "Le 'from' n'est pas un numéro Twilio valide de votre compte, "
           "ou n'est pas activé pour le SMS.",
    21610: "Destinataire désabonné (STOP). Envoi bloqué par Twilio.",
    21612: "Twilio ne peut pas router le message vers ce numéro depuis ce 'from'.",
    21614: "Le numéro destinataire n'est pas un mobile valide.",
    21617: "Le corps du message dépasse la longueur maximale autorisée.",
    21660: "Incohérence : 'from' / 'messaging_service_sid'.",
    21266: "Sender ID alphanumérique non autorisé/non enregistré pour ce pays.",
    30450: "Sender ID alphanumérique non enregistré pour la destination.",
    63007: "Twilio n'a pas trouvé de canal d'envoi pour ce 'from'.",
    21408: "Permission géographique manquante pour ce pays (console Twilio).",
    20003: "Authentification échouée : ACCOUNT_SID / AUTH_TOKEN incorrects.",
}


# ===========================================================================
# Modèles de données
# ===========================================================================
@dataclass
class MessageStats:
    encoding: str
    units: int          # septets (GSM-7) ou code units UTF-16 (UCS-2)
    segments: int
    per_segment: int
    single_limit: int


@dataclass
class SendResult:
    to: str
    success: bool
    sid: Optional[str] = None
    status: Optional[str] = None
    price: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[int] = None


# ===========================================================================
# Utilitaires : analyse du message, validation
# ===========================================================================
def analyze_message(text: str) -> MessageStats:
    """Détermine l'encodage (GSM-7 vs Unicode) et le nombre de segments."""
    is_gsm = all((c in GSM7_BASIC) or (c in GSM7_EXTENDED) for c in text)

    if is_gsm:
        units = sum(2 if c in GSM7_EXTENDED else 1 for c in text)
        single_limit, per_segment, encoding = 160, 153, "GSM-7"
    else:
        # Code units UTF-16 : un emoji hors BMP compte pour 2.
        units = len(text.encode("utf-16-le")) // 2
        single_limit, per_segment, encoding = 70, 67, "Unicode (UCS-2)"

    if units == 0:
        segments = 0
    elif units <= single_limit:
        segments = 1
    else:
        segments = math.ceil(units / per_segment)

    return MessageStats(encoding, units, segments, per_segment, single_limit)


def looks_like_phone(value: str) -> bool:
    """Heuristique : ressemble à un numéro (commence par + et chiffres)."""
    v = value.strip().replace(" ", "")
    return v.startswith("+") and v[1:].isdigit()


def validate_recipient(raw: str) -> tuple[bool, str, str]:
    """
    Valide/normalise un numéro destinataire.
    Retourne (ok, numero_e164, info_texte).
    """
    value = raw.strip().replace(" ", "")

    if _HAS_PHONENUMBERS:
        try:
            num = phonenumbers.parse(value, None)  # None => le + est requis
            if not phonenumbers.is_valid_number(num):
                return False, value, "numéro invalide selon phonenumbers"
            e164 = phonenumbers.format_number(
                num, phonenumbers.PhoneNumberFormat.E164
            )
            region = geocoder.description_for_number(num, "fr") or "?"
            op = carrier.name_for_number(num, "fr") or ""
            info = f"{region}" + (f" · {op}" if op else "")
            return True, e164, info
        except NumberParseException as exc:
            return False, value, f"format invalide ({exc})"

    # Fallback sans phonenumbers : validation E.164 minimale.
    if looks_like_phone(value) and 8 <= len(value) <= 16:
        return True, value, "format E.164 (validation basique)"
    return False, value, "doit être au format E.164, ex: +33612345678"


def validate_sender_id(raw: str) -> tuple[bool, str, str]:
    """
    Valide un sender ID : soit un numéro E.164, soit un alpha sender (<=11 car.).
    Retourne (ok, valeur, type_texte).
    """
    value = raw.strip()
    if not value:
        return False, value, "vide"

    if looks_like_phone(value):
        ok, e164, _ = validate_recipient(value)
        if ok:
            return True, e164, "numéro (long code)"
        return False, value, "numéro mal formé"

    # Alpha sender : 1 à 11 caractères, doit contenir au moins une lettre,
    # caractères alphanumériques + quelques symboles tolérés, pas d'espace en
    # début/fin. Twilio recommande [A-Za-z0-9] et espaces internes.
    if len(value) > ALPHA_MAX_LEN:
        return False, value, f"alpha sender > {ALPHA_MAX_LEN} caractères"
    if not any(c.isalpha() for c in value):
        return False, value, "alpha sender doit contenir au moins une lettre"
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 "
    )
    if not all(c in allowed for c in value):
        return False, value, "caractères non autorisés (A-Z a-z 0-9 espace)"
    return True, value, "alpha sender (one-way, peut nécessiter enregistrement)"


# ===========================================================================
# Gestion des identifiants Twilio
# ===========================================================================
def load_credentials() -> dict[str, Optional[str]]:
    """Charge les identifiants depuis .env puis les variables d'environnement.

    Deux méthodes d'authentification sont supportées :
    - API Key (recommandée) : TWILIO_API_KEY_SID (SK...) + TWILIO_API_KEY_SECRET
      + TWILIO_ACCOUNT_SID (AC...)
    - Auth Token (classique) : TWILIO_ACCOUNT_SID (AC...) + TWILIO_AUTH_TOKEN
    """
    if os.path.exists(ENV_PATH):
        load_dotenv(ENV_PATH)
    return {
        "account_sid": os.getenv("TWILIO_ACCOUNT_SID"),
        "auth_token": os.getenv("TWILIO_AUTH_TOKEN"),
        "api_key_sid": os.getenv("TWILIO_API_KEY_SID"),
        "api_key_secret": os.getenv("TWILIO_API_KEY_SECRET"),
    }


def setup_credentials_wizard() -> dict[str, Optional[str]]:
    """Assistant de configuration : demande et sauvegarde les identifiants."""
    console.print(
        Panel(
            "Identifiants Twilio introuvables.\n\n"
            "Récupérez-les sur [link]https://console.twilio.com[/link].\n\n"
            "[bold]Méthode 1 — API Key[/bold] (recommandée, révocable) :\n"
            "  · Account SID  (AC...) — sur le dashboard\n"
            "  · API Key SID  (SK...) + secret — Account > API keys & tokens\n\n"
            "[bold]Méthode 2 — Auth Token[/bold] (classique) :\n"
            "  · Account SID  (AC...) + Auth Token — sur le dashboard\n\n"
            "Les valeurs seront enregistrées dans un fichier [bold].env[/bold] local.",
            title="🔐  Configuration",
            border_style="yellow",
        )
    )
    use_api_key = Confirm.ask(
        "Utiliser une API Key (SK...) ? [dim](Non = Auth Token)[/dim]", default=True
    )

    account_sid = Prompt.ask("[cyan]TWILIO_ACCOUNT_SID[/cyan] (AC...)").strip()
    creds: dict[str, Optional[str]] = {
        "account_sid": account_sid,
        "auth_token": None,
        "api_key_sid": None,
        "api_key_secret": None,
    }

    if use_api_key:
        creds["api_key_sid"] = Prompt.ask("[cyan]TWILIO_API_KEY_SID[/cyan] (SK...)").strip()
        creds["api_key_secret"] = Prompt.ask(
            "[cyan]TWILIO_API_KEY_SECRET[/cyan]", password=True
        ).strip()
    else:
        creds["auth_token"] = Prompt.ask(
            "[cyan]TWILIO_AUTH_TOKEN[/cyan]", password=True
        ).strip()

    if Confirm.ask("Sauvegarder dans .env ?", default=True):
        if not os.path.exists(ENV_PATH):
            open(ENV_PATH, "a").close()
        set_key(ENV_PATH, "TWILIO_ACCOUNT_SID", creds["account_sid"] or "")
        if use_api_key:
            set_key(ENV_PATH, "TWILIO_API_KEY_SID", creds["api_key_sid"] or "")
            set_key(ENV_PATH, "TWILIO_API_KEY_SECRET", creds["api_key_secret"] or "")
        else:
            set_key(ENV_PATH, "TWILIO_AUTH_TOKEN", creds["auth_token"] or "")
        console.print(f"[green]✓[/green] Identifiants enregistrés dans {ENV_PATH}")
        _ensure_gitignore()

    return creds


def _ensure_gitignore() -> None:
    """Ajoute .env au .gitignore pour éviter de committer les secrets."""
    gi_path = os.path.join(os.path.dirname(ENV_PATH), ".gitignore")
    line = ".env"
    try:
        existing = ""
        if os.path.exists(gi_path):
            with open(gi_path, "r", encoding="utf-8") as fh:
                existing = fh.read()
        if line not in existing.split():
            with open(gi_path, "a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(line + "\n")
    except OSError:
        pass  # non bloquant


def _build_client(creds: dict[str, Optional[str]]) -> Optional[Client]:
    """Construit un client Twilio selon les identifiants disponibles, ou None."""
    account_sid = creds.get("account_sid")
    # Méthode API Key : Client(api_key_sid, api_key_secret, account_sid)
    if creds.get("api_key_sid") and creds.get("api_key_secret") and account_sid:
        return Client(
            creds["api_key_sid"], creds["api_key_secret"], account_sid
        )
    # Méthode Auth Token : Client(account_sid, auth_token)
    if account_sid and creds.get("auth_token"):
        return Client(account_sid, creds["auth_token"])
    return None


def get_client() -> Client:
    """Construit un client Twilio authentifié, avec assistant si besoin."""
    client = _build_client(load_credentials())
    if client is None:
        client = _build_client(setup_credentials_wizard())
    if client is None:
        console.print(
            "[red]Identifiants incomplets.[/red] Il faut soit "
            "[cyan]Account SID + API Key SID + secret[/cyan], soit "
            "[cyan]Account SID + Auth Token[/cyan]. Abandon."
        )
        sys.exit(1)
    return client


# ===========================================================================
# Appels Twilio (compte, numéros, statut)
# ===========================================================================
def show_account_info(client: Client) -> None:
    """Affiche les informations du compte et le solde."""
    try:
        acct = client.api.accounts(client.account_sid).fetch()
        table = Table(box=box.SIMPLE, show_header=False)
        table.add_column("k", style="cyan")
        table.add_column("v", style="white")
        table.add_row("Compte", acct.friendly_name)
        table.add_row("SID", acct.sid)
        status_color = "green" if acct.status == "active" else "yellow"
        table.add_row("Statut", f"[{status_color}]{acct.status}[/{status_color}]")
        table.add_row("Type", acct.type)  # "Trial" ou "Full"

        try:
            bal = client.balance.fetch()
            table.add_row("Solde", f"{bal.balance} {bal.currency}")
        except TwilioException:
            pass

        console.print(Panel(table, title="👤  Compte Twilio", border_style="blue"))

        if str(acct.type).lower() == "trial":
            console.print(
                "[yellow]ℹ️  Compte d'essai (trial)[/yellow] : vous ne pouvez "
                "envoyer qu'aux numéros vérifiés, et un préfixe Twilio est "
                "ajouté au message. Les alpha senders sont limités.\n"
            )
    except TwilioRestException as exc:
        _print_twilio_error(exc)


def list_phone_numbers(client: Client) -> list[str]:
    """Liste les numéros Twilio du compte (capables d'envoyer des SMS)."""
    numbers: list[str] = []
    try:
        records = client.incoming_phone_numbers.list(limit=50)
    except TwilioRestException as exc:
        _print_twilio_error(exc)
        return numbers

    if not records:
        console.print("[yellow]Aucun numéro Twilio sur ce compte.[/yellow]")
        return numbers

    table = Table(title="📞  Vos numéros Twilio", box=box.ROUNDED)
    table.add_column("#", style="dim", justify="right")
    table.add_column("Numéro", style="cyan")
    table.add_column("Nom", style="white")
    table.add_column("SMS", justify="center")
    for i, rec in enumerate(records, 1):
        sms_ok = "[green]✓[/green]" if rec.capabilities.get("sms") else "[red]✗[/red]"
        table.add_row(str(i), rec.phone_number, rec.friendly_name or "—", sms_ok)
        numbers.append(rec.phone_number)
    console.print(table)
    return numbers


def check_message_status(client: Client, sid: str) -> None:
    """Récupère et affiche le statut d'un message déjà envoyé."""
    try:
        msg = client.messages(sid.strip()).fetch()
    except TwilioRestException as exc:
        _print_twilio_error(exc)
        return

    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("k", style="cyan")
    table.add_column("v", style="white")
    table.add_row("SID", msg.sid)
    table.add_row("De", msg.from_ or "—")
    table.add_row("Vers", msg.to)
    table.add_row("Statut", _color_status(msg.status))
    table.add_row("Segments", str(msg.num_segments))
    table.add_row("Prix", f"{msg.price or '—'} {msg.price_unit or ''}".strip())
    if msg.error_code:
        table.add_row("Erreur", f"[red]{msg.error_code} — {msg.error_message}[/red]")
    console.print(Panel(table, title="📬  Statut du message", border_style="blue"))


# ===========================================================================
# Envoi
# ===========================================================================
def send_sms(
    client: Client,
    sender: str,
    recipients: list[str],
    body: str,
    dry_run: bool = False,
) -> list[SendResult]:
    """Envoie le message à chaque destinataire et retourne les résultats."""
    results: list[SendResult] = []
    for to in recipients:
        if dry_run:
            results.append(
                SendResult(to=to, success=True, sid="(dry-run)", status="non envoyé")
            )
            continue
        try:
            msg = client.messages.create(body=body, from_=sender, to=to)
            results.append(
                SendResult(
                    to=to,
                    success=True,
                    sid=msg.sid,
                    status=msg.status,
                    price=f"{msg.price or '?'} {msg.price_unit or ''}".strip(),
                )
            )
        except TwilioRestException as exc:
            results.append(
                SendResult(
                    to=to,
                    success=False,
                    error=exc.msg,
                    error_code=exc.code,
                )
            )
    return results


def render_results(results: list[SendResult]) -> None:
    """Affiche le tableau des résultats d'envoi."""
    table = Table(title="📨  Résultats", box=box.ROUNDED)
    table.add_column("Destinataire", style="cyan")
    table.add_column("Résultat", justify="center")
    table.add_column("SID / Détail", style="dim")

    for r in results:
        if r.success:
            detail = r.sid or ""
            if r.status:
                detail += f"  ({_color_status(r.status)})"
            table.add_row(r.to, "[green]✓ envoyé[/green]", detail)
        else:
            table.add_row(r.to, "[red]✗ échec[/red]", f"[red]{r.error}[/red]")
    console.print(table)

    # Aide pour les erreurs rencontrées
    seen_codes = {r.error_code for r in results if r.error_code}
    for code in seen_codes:
        hint = TWILIO_ERROR_HINTS.get(code)
        if hint:
            console.print(
                f"[yellow]Code {code}[/yellow] : {hint}\n"
                f"[dim]→ https://www.twilio.com/docs/api/errors/{code}[/dim]"
            )


# ===========================================================================
# Helpers d'affichage
# ===========================================================================
def _color_status(status: str) -> str:
    colors = {
        "delivered": "green",
        "sent": "green",
        "queued": "yellow",
        "sending": "yellow",
        "accepted": "yellow",
        "undelivered": "red",
        "failed": "red",
    }
    return f"[{colors.get(status, 'white')}]{status}[/{colors.get(status, 'white')}]"


def _print_twilio_error(exc: TwilioRestException) -> None:
    console.print(
        Panel(
            f"[red]{exc.msg}[/red]\n\n"
            f"Code : [bold]{exc.code}[/bold]  ·  Statut HTTP : {exc.status}\n"
            + (
                f"[yellow]{TWILIO_ERROR_HINTS[exc.code]}[/yellow]\n"
                if exc.code in TWILIO_ERROR_HINTS
                else ""
            )
            + f"[dim]→ https://www.twilio.com/docs/api/errors/{exc.code}[/dim]",
            title="❌  Erreur Twilio",
            border_style="red",
        )
    )


def _banner() -> None:
    console.print(
        Panel(
            Text.from_markup(
                "[bold cyan]Twilio SMS Panel[/bold cyan]\n"
                "[dim]Envoi de SMS avec sender ID personnalisé — API officielle Twilio[/dim]"
            ),
            border_style="cyan",
            box=box.DOUBLE,
        )
    )


def _print_message_preview(body: str) -> None:
    stats = analyze_message(body)
    info = (
        f"Encodage : [bold]{stats.encoding}[/bold]  ·  "
        f"{stats.units} unités  ·  "
        f"[bold]{stats.segments}[/bold] segment(s)  "
        f"(limite {stats.single_limit} / {stats.per_segment} par segment)"
    )
    if stats.segments > 1:
        info += "\n[yellow]⚠️  Message multi-segments : facturé par segment.[/yellow]"
    console.print(Panel(body or "[dim](vide)[/dim]", title="✉️  Aperçu", border_style="green"))
    console.print(info + "\n")


# ===========================================================================
# Flux interactif (le « panel »)
# ===========================================================================
def interactive_send(client: Client) -> None:
    """Compose et envoie un SMS pas à pas."""
    # --- 1. Sender ID ---
    console.print("[bold]1) Expéditeur (sender ID)[/bold]")
    console.print(
        "[dim]Tapez un alpha sender (ex: MonService, max 11 car.), un numéro "
        "(+336...), ou 'list' pour choisir un numéro de votre compte.[/dim]"
    )
    sender = ""
    while not sender:
        raw = Prompt.ask("Expéditeur").strip()
        if raw.lower() == "list":
            nums = list_phone_numbers(client)
            if nums:
                idx = IntPrompt.ask(
                    "Numéro à utiliser (#)", default=1, choices=[str(i) for i in range(1, len(nums) + 1)]
                )
                sender = nums[idx - 1]
                console.print(f"[green]✓[/green] Expéditeur : {sender}")
            continue
        ok, value, kind = validate_sender_id(raw)
        if ok:
            sender = value
            console.print(f"[green]✓[/green] Expéditeur : [cyan]{sender}[/cyan] [dim]({kind})[/dim]")
            if not looks_like_phone(sender):
                console.print(
                    "[yellow]ℹ️  Alpha sender : non réponse possible, et "
                    "enregistrement requis dans certains pays (France, US...).[/yellow]"
                )
        else:
            console.print(f"[red]✗ Invalide : {kind}[/red]")

    # --- 2. Destinataires ---
    console.print("\n[bold]2) Destinataire(s)[/bold]")
    console.print("[dim]Format E.164 (+33612345678). Séparez par des virgules pour plusieurs.[/dim]")
    recipients: list[str] = []
    while not recipients:
        raw = Prompt.ask("Destinataire(s)").strip()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            ok, e164, info = validate_recipient(part)
            if ok:
                recipients.append(e164)
                console.print(f"  [green]✓[/green] {e164} [dim]({info})[/dim]")
            else:
                console.print(f"  [red]✗ {part} — {info}[/red]")
        if not recipients:
            console.print("[red]Aucun destinataire valide.[/red]")

    # --- 3. Message ---
    console.print("\n[bold]3) Message[/bold]")
    body = ""
    while not body:
        body = Prompt.ask("Message").strip()
        if not body:
            console.print("[red]Le message ne peut pas être vide.[/red]")
    _print_message_preview(body)

    # --- 4. Récapitulatif + confirmation ---
    recap = Table(box=box.SIMPLE, show_header=False)
    recap.add_column("k", style="cyan")
    recap.add_column("v", style="white")
    recap.add_row("Expéditeur", sender)
    recap.add_row("Destinataires", f"{len(recipients)} : " + ", ".join(recipients))
    stats = analyze_message(body)
    recap.add_row("Segments / msg", f"{stats.segments} ({stats.encoding})")
    recap.add_row("Total segments", str(stats.segments * len(recipients)))
    console.print(Panel(recap, title="🧾  Récapitulatif", border_style="magenta"))

    if not Confirm.ask("[bold]Envoyer maintenant ?[/bold]", default=False):
        console.print("[yellow]Annulé.[/yellow]")
        return

    # --- 5. Envoi ---
    with console.status("[cyan]Envoi en cours...[/cyan]"):
        results = send_sms(client, sender, recipients, body)
    render_results(results)


def main_menu(client: Client) -> None:
    """Boucle principale du panel."""
    show_account_info(client)
    while True:
        console.print(
            Panel(
                "[bold]1[/bold]  Envoyer un SMS\n"
                "[bold]2[/bold]  Lister mes numéros Twilio\n"
                "[bold]3[/bold]  Vérifier le statut d'un message\n"
                "[bold]4[/bold]  Infos du compte\n"
                "[bold]5[/bold]  Quitter",
                title="📋  Menu",
                border_style="cyan",
            )
        )
        choice = Prompt.ask("Choix", choices=["1", "2", "3", "4", "5"], default="1")
        console.print()
        if choice == "1":
            interactive_send(client)
        elif choice == "2":
            list_phone_numbers(client)
        elif choice == "3":
            sid = Prompt.ask("SID du message (SM...)").strip()
            if sid:
                check_message_status(client, sid)
        elif choice == "4":
            show_account_info(client)
        elif choice == "5":
            console.print("[dim]À bientôt 👋[/dim]")
            break
        console.print()


# ===========================================================================
# Mode CLI non-interactif
# ===========================================================================
def run_cli(args: argparse.Namespace) -> int:
    client = get_client()

    ok_s, sender, kind = validate_sender_id(args.sender)
    if not ok_s:
        console.print(f"[red]Expéditeur invalide : {kind}[/red]")
        return 2

    recipients: list[str] = []
    for part in args.to.split(","):
        part = part.strip()
        if not part:
            continue
        ok_r, e164, info = validate_recipient(part)
        if ok_r:
            recipients.append(e164)
        else:
            console.print(f"[red]Destinataire invalide : {part} — {info}[/red]")
    if not recipients:
        console.print("[red]Aucun destinataire valide.[/red]")
        return 2

    _print_message_preview(args.body)
    console.print(
        f"Expéditeur : [cyan]{sender}[/cyan]  →  {len(recipients)} destinataire(s)"
    )
    if args.dry_run:
        console.print("[yellow]Mode --dry-run : aucun message ne sera envoyé.[/yellow]")

    results = send_sms(client, sender, recipients, args.body, dry_run=args.dry_run)
    render_results(results)
    return 0 if all(r.success for r in results) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Panel d'envoi de SMS via l'API Twilio (sender ID personnalisé).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--to", help="Destinataire(s) E.164, séparés par des virgules.")
    p.add_argument(
        "--from", dest="sender",
        help="Sender ID : numéro Twilio (+336...) ou alpha sender (max 11 car.).",
    )
    p.add_argument("--body", help="Contenu du message.")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Simule l'envoi (validation + aperçu) sans appeler Twilio.",
    )
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Si les 3 arguments d'envoi sont fournis → mode CLI direct.
    if args.to and args.sender and args.body:
        return run_cli(args)

    # Sinon : panel interactif.
    if any([args.to, args.sender, args.body]):
        console.print(
            "[yellow]Pour l'envoi direct, fournissez --to, --from ET --body. "
            "Passage en mode interactif.[/yellow]\n"
        )

    _banner()
    try:
        client = get_client()
        main_menu(client)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrompu.[/dim]")
        return 130
    except TwilioException as exc:
        console.print(f"[red]Erreur Twilio : {exc}[/red]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

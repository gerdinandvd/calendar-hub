"""UI phrases. English is the default; Dutch is the second catalog."""

EN = {
    "brand": "Calendar hub",
    "lang.en": "English",
    "lang.nl": "Nederlands",
    "nav.home": "Home",
    "nav.sources": "Sources",
    "nav.calendars": "Calendars",
    "nav.sync": "Sync",
    "nav.health": "Health",
    "home.intro": "This hub keeps sources and sync in one place.",
    "empty.sources": "There is no source yet.",
    "action.add_source": "Add a source",
    "action.add_ics": "Add an external ICS",
    "action.add_google": "Add a Google calendar",
    "action.add_webhook": "Add a webhook",
    "action.edit_source": "Edit source",
    "action.save": "Save",
    "action.edit": "Edit",
    "action.sync": "Sync now",
    "action.delete": "Delete",
    "action.cancel": "Cancel",
    "action.turn_on": "Turn on",
    "action.turn_off": "Turn off",
    "state.on": "On",
    "state.off": "Off",
    "dialog.delete_source": "Delete source",
    "dialog.delete_body": "Deleting this source also deletes its calendar.",
    "kind.private": "Private",
    "kind.google": "Google",
    "kind.webhook": "Webhook",
    "kind.ics": "External ICS",
    "status.idle": "not started",
    "status.running": "running",
    "status.ok": "succeeded",
    "status.error": "error",
    "status.na": "not applicable",
    "color.blue": "blue",
    "color.green": "green",
    "color.purple": "purple",
    "color.yellow": "yellow",
    "probe.ok": "ok",
    "probe.error": "error",
    "column.name": "Name",
    "column.kind": "Kind",
    "column.status": "Status",
    "column.enabled": "Enabled",
    "column.last_success": "Last success",
    "column.last_error": "Last error",
    "column.error": "Error",
    "column.seen": "Seen",
    "column.upserted": "Upserted",
    "column.deleted": "Deleted",
    "column.sync": "Sync",
    "field.name": "Name",
    "field.timezone": "Time zone",
    "field.url": "URL",
    "field.authorization": "Authorization header",
    "field.authorization_set": "An authorization header is set.",
    "field.clear_authorization": "Remove the authorization header",
    "field.poll": "Poll interval in seconds",
    "field.allow_http": "Allow http",
    "field.calendar_id": "Calendar id",
    "caldav.title": "CalDAV",
    "caldav.password": "The password is the password of HUB_USER.",
    "caldav.host": "Host",
    "caldav.username": "Username",
    "caldav.android": "Android uses DAVx⁵ to reach the calendar app on the device. DAVx⁵ syncs and the calendar app shows the event.",
    "caldav.ios": "iOS uses the calendar app.",
    "caldav.thunderbird": "Thunderbird connects directly to CalDAV.",
    "webhook.endpoint": "Endpoint",
    "webhook.once": "This secret is shown once.",
    "webhook.set": "Secret is set.",
    "webhook.rotate": "Rotate secret",
    "health.sqlite": "SQLite",
    "health.radicale": "Radicale",
    "health.litestream": "Litestream heartbeat",
    "health.backup": "Backup heartbeat",
    "health.disk": "Free disk",
    "sentence.enter_name": "Enter a name.",
    "sentence.timezone": "Enter a valid time zone.",
    "sentence.poll": "Enter a poll interval in seconds.",
    "sentence.https": "Enter an https URL.",
    "sentence.authorization": "Enter an authorization header.",
    "sentence.calendar_id": "Enter a calendar id.",
    "sentence.name_used": "That name is already in use.",
    "sentence.max_sources": "You can add at most 20 sources.",
    "sentence.calendar_saved": "The calendar could not be saved.",
    "sentence.calendar_deleted": "The calendar could not be deleted.",
    "sentence.not_found": "Not found.",
    "sentence.confirm_delete": "Deletion requires confirmation.",
    "sentence.sync_running": "A sync is already running.",
    "sentence.rotate_webhook": "Only a webhook source can rotate a secret.",
    "sentence.enabled": "Enabled must be 0 or 1.",
    "sentence.allow_http": "Allow http must be 0 or 1.",
    "sentence.creatable": "Only a private, webhook, external ICS, or Google source can be created.",
    "sentence.fields_ics": "Those fields are only for an external ICS source.",
    "sentence.fields_google_or_ics": "Those fields are only for a Google or external ICS source.",
    "sentence.fields_google": "Those fields are only for a Google source.",
    "sentence.sync_unused": "Sync is not used for this source.",
    "sentence.sync_off": "Sync is turned off for this source.",
    "sentence.secret_key": "The secret key is not configured.",
    "sentence.radicale": "Radicale is not reachable.",
    "sentence.litestream": "The Litestream heartbeat is older than 5 minutes.",
    "sentence.backup": "The backup is older than 8 hours.",
    "sentence.google_incomplete": "The Google connection could not be completed.",
    "sentence.google_write": "The Google connection can write, so it was not saved.",
    "sentence.google_config": "Google is not configured.",
    "sentence.google_disconnected": "Google is not connected.",
    "sentence.connect": "Connect. This connection only reads and does not write back.",
    "sentence.reconnect": "Reconnect. This connection only reads and does not write back.",
    "sentence.event_saved": "The event could not be saved.",
    "sentence.capacity": "This calendar already has 20000 events.",
    "sentence.process_stopped": "The process stopped.",
    "sentence.feed_fetch": "The calendar feed could not be fetched.",
    "sentence.feed_timeout": "The calendar feed did not respond in time.",
    "sentence.feed_size": "The calendar feed is larger than 20 MB.",
    "sentence.feed_read": "The calendar feed could not be read.",
    "sentence.google_fetch": "The Google calendar could not be fetched.",
    "sentence.google_token": "The Google access token could not be refreshed.",
    "sentence.google_read": "The Google calendar could not be read.",
}

NL = {
    "brand": "Calendar hub",
    "lang.en": "English",
    "lang.nl": "Nederlands",
    "nav.home": "Home",
    "nav.sources": "Bronnen",
    "nav.calendars": "Kalenders",
    "nav.sync": "Sync",
    "nav.health": "Gezondheid",
    "home.intro": "Deze hub houdt bronnen en synchronisatie op één plek.",
    "empty.sources": "Er is nog geen bron.",
    "action.add_source": "Bron toevoegen",
    "action.add_ics": "Externe ICS toevoegen",
    "action.add_google": "Google-agenda toevoegen",
    "action.add_webhook": "Webhook toevoegen",
    "action.edit_source": "Bron wijzigen",
    "action.save": "Opslaan",
    "action.edit": "Wijzigen",
    "action.sync": "Nu synchroniseren",
    "action.delete": "Verwijderen",
    "action.cancel": "Annuleren",
    "action.turn_on": "Aanzetten",
    "action.turn_off": "Uitzetten",
    "state.on": "Aan",
    "state.off": "Uit",
    "dialog.delete_source": "Bron verwijderen",
    "dialog.delete_body": "Het verwijderen van deze bron verwijdert ook de kalender.",
    "kind.private": "Privé",
    "kind.google": "Google",
    "kind.webhook": "Webhook",
    "kind.ics": "Externe ICS",
    "status.idle": "nog niet gestart",
    "status.running": "bezig",
    "status.ok": "gelukt",
    "status.error": "fout",
    "status.na": "niet van toepassing",
    "color.blue": "blauw",
    "color.green": "groen",
    "color.purple": "paars",
    "color.yellow": "geel",
    "probe.ok": "ok",
    "probe.error": "fout",
    "column.name": "Naam",
    "column.kind": "Soort",
    "column.status": "Status",
    "column.enabled": "Ingeschakeld",
    "column.last_success": "Laatste succes",
    "column.last_error": "Laatste fout",
    "column.error": "Fout",
    "column.seen": "Gezien",
    "column.upserted": "Bijgewerkt",
    "column.deleted": "Verwijderd",
    "column.sync": "Sync",
    "field.name": "Naam",
    "field.timezone": "Tijdzone",
    "field.url": "URL",
    "field.authorization": "Autorisatieheader",
    "field.authorization_set": "Er is een autorisatieheader ingesteld.",
    "field.clear_authorization": "Autorisatieheader verwijderen",
    "field.poll": "Poll-interval in seconden",
    "field.allow_http": "Http toestaan",
    "field.calendar_id": "Agenda-id",
    "caldav.title": "CalDAV",
    "caldav.password": "Het wachtwoord is het wachtwoord van HUB_USER.",
    "caldav.host": "Host",
    "caldav.username": "Gebruikersnaam",
    "caldav.android": "Android loopt via DAVx⁵ naar de agenda-app van het toestel. DAVx⁵ synchroniseert en de agenda-app toont het event.",
    "caldav.ios": "iOS loopt via de agenda-app.",
    "caldav.thunderbird": "Thunderbird staat rechtstreeks op CalDAV.",
    "webhook.endpoint": "Eindpunt",
    "webhook.once": "Dit geheim wordt één keer getoond.",
    "webhook.set": "Geheim is gezet.",
    "webhook.rotate": "Geheim roteren",
    "health.sqlite": "SQLite",
    "health.radicale": "Radicale",
    "health.litestream": "Litestream-hartslag",
    "health.backup": "Backup-hartslag",
    "health.disk": "Vrije schijf",
    "sentence.enter_name": "Vul een naam in.",
    "sentence.timezone": "Vul een geldige tijdzone in.",
    "sentence.poll": "Vul een poll-interval in seconden in.",
    "sentence.https": "Vul een https-URL in.",
    "sentence.authorization": "Vul een autorisatieheader in.",
    "sentence.calendar_id": "Vul een agenda-id in.",
    "sentence.name_used": "Die naam is al in gebruik.",
    "sentence.max_sources": "Je kunt hoogstens 20 bronnen toevoegen.",
    "sentence.calendar_saved": "De kalender kon niet worden opgeslagen.",
    "sentence.calendar_deleted": "De kalender kon niet worden verwijderd.",
    "sentence.not_found": "Niet gevonden.",
    "sentence.confirm_delete": "Verwijderen vereist bevestiging.",
    "sentence.sync_running": "Er loopt al een synchronisatie.",
    "sentence.rotate_webhook": "Alleen een webhookbron kan een geheim roteren.",
    "sentence.enabled": "Ingeschakeld moet 0 of 1 zijn.",
    "sentence.allow_http": "Http toestaan moet 0 of 1 zijn.",
    "sentence.creatable": "Alleen een privé-, webhook-, externe ICS- of Google-bron kan worden aangemaakt.",
    "sentence.fields_ics": "Die velden zijn alleen voor een externe ICS-bron.",
    "sentence.fields_google_or_ics": "Die velden zijn alleen voor een Google- of externe ICS-bron.",
    "sentence.fields_google": "Die velden zijn alleen voor een Google-bron.",
    "sentence.sync_unused": "Synchronisatie geldt niet voor deze bron.",
    "sentence.sync_off": "Synchronisatie staat uit voor deze bron.",
    "sentence.secret_key": "De geheime sleutel is niet ingesteld.",
    "sentence.radicale": "Radicale is niet bereikbaar.",
    "sentence.litestream": "De Litestream-hartslag is ouder dan 5 minuten.",
    "sentence.backup": "De backup is ouder dan 8 uur.",
    "sentence.google_incomplete": "De Google-koppeling kon niet worden afgerond.",
    "sentence.google_write": "De Google-koppeling kan schrijven en is daarom niet opgeslagen.",
    "sentence.google_config": "Google is niet ingesteld.",
    "sentence.google_disconnected": "Google is niet gekoppeld.",
    "sentence.connect": "Koppelen. Deze koppeling leest alleen en schrijft niet terug.",
    "sentence.reconnect": "Opnieuw koppelen. Deze koppeling leest alleen en schrijft niet terug.",
    "sentence.event_saved": "Het event kon niet worden opgeslagen.",
    "sentence.capacity": "Deze kalender heeft al 20000 events.",
    "sentence.process_stopped": "Het proces is gestopt.",
    "sentence.feed_fetch": "De agendafeed kon niet worden opgehaald.",
    "sentence.feed_timeout": "De agendafeed antwoordde niet op tijd.",
    "sentence.feed_size": "De agendafeed is groter dan 20 MB.",
    "sentence.feed_read": "De agendafeed kon niet worden gelezen.",
    "sentence.google_fetch": "De Google-agenda kon niet worden opgehaald.",
    "sentence.google_token": "Het Google-toegangstoken kon niet worden ververst.",
    "sentence.google_read": "De Google-agenda kon niet worden gelezen.",
}


def translator(lang: str):
    """Resolve one catalog key. Dutch falls back to English, then to the key."""

    primary = NL if lang == "nl" else EN

    def translate(key: str) -> str:
        if key in primary:
            return primary[key]
        if key in EN:
            return EN[key]
        return key

    return translate


def _legacy_index() -> dict[str, str]:
    found: dict[str, str] = {}
    for key, phrase in EN.items():
        if not key.startswith("sentence."):
            continue
        previous = found.get(phrase)
        if previous is not None:
            raise RuntimeError(f"duplicate sentence for {previous} and {key}")
        found[phrase] = key
    return found


_LEGACY = _legacy_index()


def _keys(value: str) -> list[str] | None:
    if not value:
        return None
    parts = value.split(" ")
    if all(part in EN for part in parts):
        return parts
    return None


def english(value: str) -> str:
    """Render catalog keys as the English sentence. Other text stays as given."""

    keys = _keys(value)
    if keys is None:
        return value
    return " ".join(EN[key] for key in keys)


def present(lang: str, value: str) -> str:
    """Show a key, several keys, or a stored English sentence in the chosen language."""

    if not value:
        return value
    translate = translator(lang)
    keys = _keys(value)
    if keys is not None:
        return " ".join(translate(key) for key in keys)
    legacy = _LEGACY.get(value)
    if legacy is not None:
        return translate(legacy)
    return value

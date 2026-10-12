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
    "empty.calendars": "There is no calendar yet.",
    "empty.sync": "There is nothing to sync yet.",
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
    "action.connect_google": "Connect Google",
    "action.reconnect_google": "Reconnect Google",
    "state.on": "On",
    "state.off": "Off",
    "dialog.delete_source": "Delete source",
    "dialog.delete_body": "This removes the calendar from this hub, including its events. The Google calendar or external feed stays as it is.",
    "kind.private": "In this hub",
    "kind.google": "Google",
    "kind.webhook": "Webhook",
    "kind.ics": "External ICS",
    "status.idle": "not started",
    "status.running": "running",
    "status.ok": "succeeded",
    "status.error": "error",
    "status.na": "no sync",
    "color.blue": "blue",
    "color.green": "green",
    "color.purple": "purple",
    "color.yellow": "yellow",
    "probe.ok": "ok",
    "probe.error": "error",
    "column.name": "Name",
    "column.kind": "Type",
    "column.status": "Status",
    "column.enabled": "Enabled",
    "column.last_success": "Last success",
    "column.last_error": "Last error",
    "column.error": "Error",
    "column.seen": "Events read",
    "column.upserted": "Events written",
    "column.deleted": "Events removed",
    "column.sync": "Sync",
    "field.name": "Name",
    "field.timezone": "Time zone",
    "field.url": "URL",
    "field.authorization": "Authorization header",
    "field.authorization_set": "An authorization header is set.",
    "field.clear_authorization": "Remove the authorization header",
    "field.poll": "Poll interval in seconds",
    "field.allow_http": "Allow http",
    "field.calendar_id": "Google calendar id",
    "hint.poll": "How often this hub fetches the calendar, in seconds.",
    "hint.authorization": "Optional. Only for a feed that requires a header.",
    "hint.allow_http": "Fetch the feed without https.",
    "hint.calendar_id": "Use primary for the main calendar of the Google account.",
    "hint.private": "This calendar lives in the hub. Nothing is fetched.",
    "hint.collection": "Add this URL in your calendar app.",
    "caldav.title": "CalDAV",
    "caldav.password": "Use the password of the hub user.",
    "caldav.host": "Host",
    "caldav.username": "Username",
    "caldav.android": "On Android, add this account in DAVx⁵. The calendar app then shows the events.",
    "caldav.ios": "On iOS, add a CalDAV account in the calendar app.",
    "caldav.thunderbird": "In Thunderbird, add a calendar on the network and choose CalDAV.",
    "webhook.endpoint": "Endpoint",
    "webhook.once": "This secret is shown once. Copy it now.",
    "webhook.set": "Secret is set.",
    "webhook.rotate": "Replace secret",
    "health.sqlite": "SQLite",
    "health.radicale": "Radicale",
    "health.litestream": "Litestream last seen",
    "health.backup": "Backup last seen",
    "health.disk": "Free disk space",
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
    "sentence.enabled": "The on/off value was not accepted.",
    "sentence.allow_http": "The http choice was not accepted.",
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
    "sentence.google_write": "Google granted write access. Only read access is allowed, so the connection was not saved.",
    "sentence.google_config": "Google is not configured.",
    "sentence.google_disconnected": "Google is not connected.",
    "sentence.connect": "Connect. This connection only reads and does not write back.",
    "sentence.reconnect": "Reconnect. This connection only reads and does not write back.",
    "sentence.read_only": "This connection can only read. It does not change the Google calendar.",
    "sentence.saved": "Saved.",
    "sentence.secret_replaced": "The previous secret no longer works.",
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
    "nav.health": "Systeemstatus",
    "empty.calendars": "Er is nog geen kalender.",
    "empty.sync": "Er is nog niets om te synchroniseren.",
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
    "action.connect_google": "Google koppelen",
    "action.reconnect_google": "Google opnieuw koppelen",
    "state.on": "Aan",
    "state.off": "Uit",
    "dialog.delete_source": "Bron verwijderen",
    "dialog.delete_body": "Dit haalt de kalender uit deze hub, inclusief de afspraken. De Google-agenda of externe feed blijft bestaan.",
    "kind.private": "In deze hub",
    "kind.google": "Google",
    "kind.webhook": "Webhook",
    "kind.ics": "Externe ICS",
    "status.idle": "nog niet gestart",
    "status.running": "bezig",
    "status.ok": "gelukt",
    "status.error": "fout",
    "status.na": "geen synchronisatie",
    "color.blue": "blauw",
    "color.green": "groen",
    "color.purple": "paars",
    "color.yellow": "geel",
    "probe.ok": "in orde",
    "probe.error": "fout",
    "column.name": "Naam",
    "column.kind": "Soort",
    "column.status": "Status",
    "column.enabled": "Ingeschakeld",
    "column.last_success": "Laatste succes",
    "column.last_error": "Laatste fout",
    "column.error": "Fout",
    "column.seen": "Afspraken gelezen",
    "column.upserted": "Afspraken geschreven",
    "column.deleted": "Afspraken verwijderd",
    "column.sync": "Sync",
    "field.name": "Naam",
    "field.timezone": "Tijdzone",
    "field.url": "URL",
    "field.authorization": "Autorisatieheader",
    "field.authorization_set": "Er is een autorisatieheader ingesteld.",
    "field.clear_authorization": "Autorisatieheader verwijderen",
    "field.poll": "Poll-interval in seconden",
    "field.allow_http": "Http toestaan",
    "field.calendar_id": "Google-agenda-id",
    "hint.poll": "Hoe vaak deze hub de kalender ophaalt, in seconden.",
    "hint.authorization": "Optioneel. Alleen voor een feed die een header vereist.",
    "hint.allow_http": "Haal de feed op zonder https.",
    "hint.calendar_id": "Gebruik primary voor de hoofdagenda van het Google-account.",
    "hint.private": "Deze kalender staat in de hub. Er wordt niets opgehaald.",
    "hint.collection": "Voeg deze URL toe in je agenda-app.",
    "caldav.title": "CalDAV",
    "caldav.password": "Gebruik het wachtwoord van de hub-gebruiker.",
    "caldav.host": "Host",
    "caldav.username": "Gebruikersnaam",
    "caldav.android": "Op Android voeg je dit account toe in DAVx⁵. De agenda-app toont daarna de afspraken.",
    "caldav.ios": "Op iOS voeg je een CalDAV-account toe in de agenda-app.",
    "caldav.thunderbird": "In Thunderbird voeg je een agenda op het netwerk toe en kies je CalDAV.",
    "webhook.endpoint": "Eindpunt",
    "webhook.once": "Dit geheim wordt één keer getoond. Kopieer het nu.",
    "webhook.set": "Geheim is gezet.",
    "webhook.rotate": "Geheim vervangen",
    "health.sqlite": "SQLite",
    "health.radicale": "Radicale",
    "health.litestream": "Litestream laatst gezien",
    "health.backup": "Backup laatst gezien",
    "health.disk": "Vrije schijfruimte",
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
    "sentence.enabled": "De aan/uit-waarde is niet geaccepteerd.",
    "sentence.allow_http": "De keuze voor http is niet geaccepteerd.",
    "sentence.creatable": "Alleen een privé-, webhook-, externe ICS- of Google-bron kan worden aangemaakt.",
    "sentence.fields_ics": "Die velden zijn alleen voor een externe ICS-bron.",
    "sentence.fields_google_or_ics": "Die velden zijn alleen voor een Google- of externe ICS-bron.",
    "sentence.fields_google": "Die velden zijn alleen voor een Google-bron.",
    "sentence.sync_unused": "Deze bron wordt niet gesynchroniseerd.",
    "sentence.sync_off": "Synchronisatie staat uit voor deze bron.",
    "sentence.secret_key": "De geheime sleutel is niet ingesteld.",
    "sentence.radicale": "Radicale is niet bereikbaar.",
    "sentence.litestream": "De Litestream-hartslag is ouder dan 5 minuten.",
    "sentence.backup": "De backup is ouder dan 8 uur.",
    "sentence.google_incomplete": "De Google-koppeling kon niet worden afgerond.",
    "sentence.google_write": "Google gaf schrijfrechten. Alleen lezen is toegestaan, daarom is de koppeling niet opgeslagen.",
    "sentence.google_config": "Google is niet ingesteld.",
    "sentence.google_disconnected": "Google is niet gekoppeld.",
    "sentence.connect": "Koppelen. Deze koppeling leest alleen en schrijft niet terug.",
    "sentence.reconnect": "Opnieuw koppelen. Deze koppeling leest alleen en schrijft niet terug.",
    "sentence.read_only": "Deze koppeling kan alleen lezen. Ze wijzigt de Google-agenda niet.",
    "sentence.saved": "Opgeslagen.",
    "sentence.secret_replaced": "Het vorige geheim werkt niet meer.",
    "sentence.event_saved": "De afspraak kon niet worden opgeslagen.",
    "sentence.capacity": "Deze kalender heeft al 20000 afspraken.",
    "sentence.process_stopped": "Het proces is gestopt.",
    "sentence.feed_fetch": "De kalenderfeed kon niet worden opgehaald.",
    "sentence.feed_timeout": "De kalenderfeed antwoordde niet op tijd.",
    "sentence.feed_size": "De kalenderfeed is groter dan 20 MB.",
    "sentence.feed_read": "De kalenderfeed kon niet worden gelezen.",
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

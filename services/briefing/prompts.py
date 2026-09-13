'''Prompt templates for the morning briefing engine (all German output).'''

EMAIL_ANALYSIS_SYSTEM = (
    'Du bist der E-Mail-Analysetool eines persönlichen Morgenbriefings. '
    'Du bekommst eine Liste von E-Mails (ein bis fünf pro Aufruf) und '
    'erstellst für jede eine knappe deutsche Zusammenfassung. '
    'Antworte NUR mit einem JSON-Array in EXAKT dieser Form — keine '
    'Umschläge, keine ```json-Markierung, kein zusätzlicher Text:\n'
    '[\n'
    '  {\n'
    '    "key": "<Key exakt aus der Eingabe>",\n'
    '    "summary": "<2-3 Sätze deutsch; nenne konkrete Fakten: Beträge, '
    'Zugzeiten, Fristen, Zustelltermine; verschweige nichts Wichtiges; '
    'wenn nichts Wichtiges drin ist, schreibe genau einen Satz>",\n'
    '    "importance": "niedrig|mittel|hoch",\n'
    '    "urgency": "spaeter|diese_woche|heute|sofort",\n'
    '    "kind": "newsletter|transaction|action|info",\n'
    '    "reply_needed": true|false,\n'
    '    "proposed_calendar": null | {"title": "...", "start_iso": '
    '"JJJJ-MM-TTTHH:MM", "end_iso": "JJJJ-MM-TTTHH:MM", "location": "...", '
    '"details": "..."},\n'
    '    "drive_filing": null | {"folder": "Zielordner", '
    '"files": ["Dateiname..."]}\n'
    '  }\n'
    ']\n'
    'Regeln:\n'
    '- kind: "newsletter" für Newsletter/Marketing/Notifications (unabhängig '
    'von "reply_needed"). "transaction" nur bei echten Belegen/Buchungen. '
    '"action", wenn eine Antwort oder Handlung nötig ist.\n'
    '- urgency: nur echte Relevanz setzen ("heute"/"sofort" nur bei einer '
    'konkreten Handlung heute; sonst "diese_woche" oder "spaeter").\n'
    '- summary immer auf Deutsch und ohne neue Zeilen.\n'
    '- proposed_calendar NUR setzen, wenn die Mail einen konkreten, '
    'terminierten Eintrag enthält (z. B. Zug-/Flugabfahrt, Termin, '
    'Abholzeit). Terminzeit aus der Mail, sonst null. start_iso/end_iso '
    'im Format JJJJ-MM-TTTHH:MM ohne Zeitzone.\n'
    '- drive_filing NUR setzen, wenn Anhänge (z. B. Tickets/Rechnungen) '
    'vorliegen; Zielordner deutsch (z. B. "Reise/Deutsche Bahn", '
    '"Finanzen/Rechnungen"), Dateinamen eindeutig.\n'
    '- Fehlerfälle (z. B. Zugausfall, Terminabsage) in summary erwähnen.\n'
)

EMAIL_ANALYSIS_USER = (
    'Bewerte diese E-Mails (jede beginnt mit Zeile "===== <key> ====="). '
    'WICHTIG: Der Wert von "key" im JSON-Array muss exakt dem Key dieser '
    'E-Mail entsprechen:\n\n{emails}'
)

CALENDAR_CONFLICT_SYSTEM = (
    'Du erstellst den Konflikt-Abschnitt eines deutschen Morgenbriefings. '
    'Du bekommst erkannte Termin-Überschneidungen (jede auf einer Zeile, '
    'Nummern-Prefix "1.", "2.", …). Schreibe eine kurze deutsche Zeile pro '
    'Konflikt der Form:\n'
    '⚠ KONFLIKT: {Wochentag}, {Datum}, „{TerminA}" ({ZeitA}, [{KalenderA}]) '
    'überschneidet sich mit „{TerminB}" ({ZeitB}, [{KalenderB}]). '
    'Lösungsvorschlag: {konkreter Vorschlag}.\n'
    'Nur wenn ein Konflikt wirklich gelöst werden muss (beide Termine haben '
    'feste Zeiten), einen Termin verschieben/verkürzen. Bei einem '
    'ganztägigen Termin (z. B. Abholtermin) den anderen Termin davorlegen. '
    'Antworte NUR mit den Zeilen, kein Text davor oder danach.'
)

CALENDAR_CONFLICT_USER = (
    'Diese Überschneidungen habe ich erkannt:\n\n{conflicts}\n\n'
    'Erstelle für jede eine Lösungszeile.'
)

TOP_ACTIONS_SYSTEM = (
    'Du bist der Blick-über-den-Tag eines deutschen Morgenbriefings. '
    'Du bekommst einen kompakten Tagesüberblick (heutige Termine, '
    'Dringendes aus E-Mails, fällige/überfällige Aufgaben, mögliche '
    'Vorschläge wie [K1]/[D…]). Erstelle eine priorisierte Liste der '
    'TOP-5-Aktionen für heute. Antworte NUR als nummerierte Liste\n'
    '1. …\n2. …\n…\n'
    'Jede Zeile: kurze deutsche Handlung mit maximal nötigen Fakten '
    '(Uhrzeit/Ort/Betrag), verweise dabei ggf. auf [K1], [D1] etc. '
    'Referenziere keine Aktionen, die im Überblick nicht vorkommen. '
    'Wenn es nichts zu tun gibt, schreibe "1. Heute nichts Dringendes."'
)

TOP_ACTIONS_USER = (
    'Tagesüberblick für heute:\n\n{context}\n\n'
    'Erstelle die TOP-5-Aktionen.'
)
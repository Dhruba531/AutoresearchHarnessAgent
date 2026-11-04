"""AppleScript / JXA used by the native Mac tools.

Rules every script follows:
* Inputs arrive through the run handler's argv (JXA scripts take one JSON
  string), never by string interpolation, so data cannot become code.
* Each script carries a `relay:<name>` marker; FakeHost keys canned replies
  on it, and logs show which script ran.
* JXA scripts return JSON.
"""

JXA = "JavaScript"
AS = "AppleScript"

CALENDAR_EVENTS = r"""// relay:calendar_events
function run(argv) {
  const a = JSON.parse(argv[0]);
  const Cal = Application("Calendar");
  const from = new Date(a.from || Date.now());
  const to = new Date(from.getTime() + (a.days || 1) * 86400000);
  const out = [];
  Cal.calendars().forEach(function (c) {
    if (a.calendar && c.name() !== a.calendar) return;
    const evs = c.events.whose({_and: [{startDate: {_greaterThan: from}}, {startDate: {_lessThan: to}}]})();
    evs.forEach(function (e) {
      out.push({calendar: c.name(), title: e.summary(), start: e.startDate().toISOString(),
                end: e.endDate().toISOString(), location: e.location() || null, all_day: e.alldayEvent()});
    });
  });
  out.sort(function (x, y) { return x.start < y.start ? -1 : 1; });
  return JSON.stringify(out.slice(0, a.limit || 50));
}
"""

CALENDAR_CREATE = r"""// relay:calendar_create
function run(argv) {
  const a = JSON.parse(argv[0]);
  const Cal = Application("Calendar");
  let cal = null;
  if (a.calendar) { const m = Cal.calendars.whose({name: a.calendar})(); if (m.length) cal = m[0]; }
  if (!cal) { const w = Cal.calendars().filter(function (c) { return c.writable(); }); cal = w[0]; }
  if (!cal) throw new Error("No writable calendar");
  const props = {summary: a.title, startDate: new Date(a.start), endDate: new Date(a.end)};
  if (a.location) props.location = a.location;
  if (a.notes) props.description = a.notes;
  const ev = Cal.Event(props);
  cal.events.push(ev);
  return JSON.stringify({created: true, calendar: cal.name()});
}
"""

REMINDERS_LIST = r"""// relay:reminders_list
function run(argv) {
  const a = JSON.parse(argv[0]);
  const R = Application("Reminders");
  const lists = a.list ? R.lists.whose({name: a.list})() : R.lists();
  const out = [];
  lists.forEach(function (l) {
    l.reminders.whose({completed: false})().forEach(function (r) {
      const due = r.dueDate();
      out.push({list: l.name(), name: r.name(), due: due ? due.toISOString() : null});
    });
  });
  return JSON.stringify(out.slice(0, a.limit || 50));
}
"""

REMINDER_CREATE = r"""// relay:reminder_create
function run(argv) {
  const a = JSON.parse(argv[0]);
  const R = Application("Reminders");
  let list = R.defaultList();
  if (a.list) { const m = R.lists.whose({name: a.list})(); if (m.length) list = m[0]; }
  const props = {name: a.title};
  if (a.due) props.dueDate = new Date(a.due);
  if (a.notes) props.body = a.notes;
  list.reminders.push(R.Reminder(props));
  return JSON.stringify({created: true, list: list.name()});
}
"""

NOTES_SEARCH = r"""// relay:notes_search
function run(argv) {
  const a = JSON.parse(argv[0]);
  const N = Application("Notes");
  const hits = N.notes.whose({_or: [{name: {_contains: a.query}}, {plaintext: {_contains: a.query}}]})();
  return JSON.stringify(hits.slice(0, a.limit || 10).map(function (n) {
    return {name: n.name(), modified: n.modificationDate().toISOString(), text: n.plaintext().slice(0, a.chars || 1500)};
  }));
}
"""

NOTE_CREATE = r"""-- relay:note_create
on run argv
  set theTitle to item 1 of argv
  set theBody to item 2 of argv
  tell application "Notes"
    make new note at folder 1 of default account with properties {name:theTitle, body:theBody}
  end tell
  return "created"
end run
"""

MAIL_RECENT = r"""// relay:mail_recent
function run(argv) {
  const a = JSON.parse(argv[0]);
  const Mail = Application("Mail");
  let msgs = Mail.inbox.messages;
  if (a.query) msgs = msgs.whose({_or: [{subject: {_contains: a.query}}, {sender: {_contains: a.query}}]});
  const n = Math.min(a.limit || 15, msgs.length);
  const out = [];
  for (let i = 0; i < n; i++) {
    const m = msgs[i];
    out.push({id: m.id(), from: m.sender(), subject: m.subject(), date: m.dateReceived().toISOString(), unread: !m.readStatus()});
  }
  return JSON.stringify(out);
}
"""

MAIL_READ = r"""// relay:mail_read
function run(argv) {
  const a = JSON.parse(argv[0]);
  const Mail = Application("Mail");
  const hits = Mail.inbox.messages.whose({id: a.id})();
  if (!hits.length) throw new Error("No message with id " + a.id);
  const m = hits[0];
  return JSON.stringify({from: m.sender(), to: m.toRecipients().map(function (r) { return r.address(); }),
                         subject: m.subject(), date: m.dateReceived().toISOString(),
                         body: m.content().slice(0, a.chars || 8000)});
}
"""

MAIL_COMPOSE = r"""-- relay:mail_compose
on run argv
  set toAddr to item 1 of argv
  set theSubject to item 2 of argv
  set theBody to item 3 of argv
  set doSend to item 4 of argv
  tell application "Mail"
    set m to make new outgoing message with properties {subject:theSubject, content:theBody, visible:true}
    tell m to make new to recipient at end of to recipients with properties {address:toAddr}
    if doSend is "1" then
      send m
      return "sent"
    end if
  end tell
  return "draft created"
end run
"""

MESSAGES_SEND = r"""-- relay:messages_send
on run argv
  set theTarget to item 1 of argv
  set theText to item 2 of argv
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    send theText to participant theTarget of svc
  end tell
  return "sent"
end run
"""

FRONTMOST = r"""-- relay:frontmost
tell application "System Events"
  set p to first application process whose frontmost is true
  set appName to name of p
  set winName to ""
  try
    set winName to name of front window of p
  end try
end tell
return appName & tab & winName
"""

RUNNING_APPS = r"""-- relay:running_apps
tell application "System Events" to set names to name of every application process whose background only is false
set AppleScript's text item delimiters to linefeed
return names as text
"""

NOTIFY = r"""-- relay:notify
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
  return "ok"
end run
"""

VOLUME_GET = r"""-- relay:volume_get
set s to get volume settings
return (output volume of s as text) & tab & (output muted of s as text)
"""

VOLUME_SET = r"""-- relay:volume_set
on run argv
  set volume output volume ((item 1 of argv) as integer)
  return "ok"
end run
"""

TRASH = r"""-- relay:trash
on run argv
  tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)
  return "moved to Trash"
end run
"""

ALL_JXA = {k: v for k, v in globals().items() if isinstance(v, str) and v.startswith("// relay:")}

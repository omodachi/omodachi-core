# The clipboard (CLIP-1)

One host clipboard, shared with a paired device when **both** sides have said
so — the same shape AUTH-1 uses for the host's password prompts. The host's
half is the preference `clipboard_sync`; the device's half is a switch in its
own Settings. Either one off means nothing travels.

```
off              nothing. The routes refuse with `clipboard_sync_disabled`.
host_to_device   the device may read this clipboard and is told when it changes.
both             the device may also write this clipboard.
```

`off` is the shipped value. Read it and write it like any other preference:

```sh
omodachi-host preferences get
omodachi-host preferences set --revision 4 --clipboard-sync host_to_device
```

## Routes

| Route | Needs | Answers |
| --- | --- | --- |
| `GET /v1/clipboard` | `host_to_device` or `both` | `200 text/plain; charset=utf-8`, the clipboard's text. An empty body means nothing is copied |
| `PUT /v1/clipboard` | `both` | `200 application/json` `{"bytes", "mime"}` — a count, never the text back |

The body is the clipboard itself rather than a JSON envelope around it: what
travels is the thing being pasted, and there is nothing else to say about it.
`PUT` sends `Content-Type: text/plain`; anything else is `clipboard_not_text`.

Both directions are capped at **64 KiB of UTF-8** and refuse rather than
truncate. Images are the second phase; until then a clipboard holding only an
image answers `clipboard_not_text` rather than pretending to be empty.

## The event

```json
{"type": "clipboard.changed", "payload": {"sequence": 4, "bytes": 11, "mime": "text/plain"}}
```

Published to every subscribed paired device while `clipboard_sync` is not
`off`. **The text is not in it**, and it never will be: the event history keeps
a thousand events and every subscriber queue up to 256 more, so a clipboard in
the payload would be a copy of everything the user ever copied, retained. A
client that wants the text asks for it.

`sequence` counts what this daemon has announced, so a client that missed one
knows it did. The announcement is throttled to one every 0.5 s, and a burst
ends with a final event carrying the state that settled.

## What the host runs

`wl-paste --type text/plain --watch /bin/echo`, started only while the
preference allows it. The watched command receives the clipboard on its stdin;
`/bin/echo` reads nothing and prints one empty line, so the watcher pipe carries
a signal rather than a stream of everything copied on the desktop. The content
is then read back deliberately, with `wl-paste --no-newline`, under the same
bound as every other read. A write is `wl-copy`, and the digest of what was
written is remembered just long enough to recognise it coming back through the
watcher — otherwise a device that pasted into the host would see its own paste
arrive as news from the host.

The watcher restarts itself when the graphical session goes away and comes
back, and stops the moment the preference is set to `off`.

## What is not kept

Nothing. The text is not written to the journal, to a log line, or into the
event. `ClipboardService.trace` keeps the last 32 movements as
`{direction, bytes, ts}` — which way it went, how much of it there was, and
when — and that is the whole record.

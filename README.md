# voice-mcp-speechkit

**English** · [Русский](README.ru.md)

A voice MCP server: your agent hears you and answers out loud in Russian.

One Python file, zero third-party libraries. Plugs into any MCP client — Claude
Code, Claude Desktop and others. Speech recognition and synthesis are done by
[Yandex SpeechKit](https://yandex.cloud/en/services/speechkit), for which
Russian is the native language.

---

## Why

Text chat with an agent is fine when you are at your desk. But some work is
easier to discuss out loud — going through a task list with your hands busy, or
when the wording comes faster spoken than typed.

Off-the-shelf voice add-ons for agents usually rely on English-first synthesis
engines. This server is built for Russian, and for weak hardware: it was
developed and run daily on a two-core 2011 desktop.

Nothing in it is Russian-only by design, though. Point `lang` and `voice` at
another locale SpeechKit supports and the same four tools work.

---

## What it does

| Tool | What it does |
|---|---|
| `voice_check` | Diagnostics: is the microphone visible, is ffmpeg present, does SpeechKit answer. Records nothing, says nothing |
| `listen` | Records from the microphone and returns the recognised text |
| `speak` | Says a text out loud |
| `converse` | Says a phrase and immediately records the reply — in one action |

### Why `converse` exists separately

If you call `speak` and then `listen` as two tool calls, a full model round trip
happens in between, and the human waits ten to fifteen seconds for the recording
cue after the agent has finished speaking. By then they have usually started
talking into a closed microphone.

In `converse` the speech, the cue and the recording run back to back inside a
single call. The pause disappears and a conversation starts behaving like one.
Use `converse` for dialogue; keep `speak` and `listen` for the cases where you
genuinely only need one half.

### The bells

The start of the recording window is marked by a high tone, the end by a low
one. Without them it is not clear when to talk — while one side waits for a cue,
the recording window is already closing. Two short beeps turned out to matter
more for usability than anything else in this server.

---

## Requirements

- **Python 3.9+** — standard library only
- **ffmpeg** in `PATH` — used for recording
- **Windows** for playback through `winsound`; on other systems the audio is
  played with `ffplay`, which ships with ffmpeg
- A **Yandex Cloud** account with a service account for SpeechKit

No Python packages to install. That is deliberate: the server is meant for
machines where you do not want to maintain a heavy environment.

---

## Installation

### 1. SpeechKit access

SpeechKit is a **Yandex Cloud** service, not a Yandex ID one. OAuth tokens from
other Yandex products do not work here; you need a separate service account.

In the [Yandex Cloud console](https://console.yandex.cloud):

1. Create a folder or take an existing one. Its identifier, shaped like
   `b1g...`, is your `YC_FOLDER_ID`.
2. Create a service account, for example `speechkit-voice`.
3. Grant it two roles:
   - `ai.speechkit-stt.user` — recognition
   - `ai.speechkit-tts.user` — synthesis
4. Create an **API key** for it. When creating the key, select the scopes
   `yc.ai.speechkitStt.execute` and `yc.ai.speechkitTts.execute`. The key value
   is shown exactly once.

**A common trap:** roles selected in the service-account creation dialog
sometimes do not get saved. Check the account list — if the roles column shows a
dash, the roles were not applied. Assign them separately, through the folder's
access-management page.

### 2. The credentials file

Copy `yandex-speechkit.env.example` to `yandex-speechkit.env` next to the
server and fill it in:

```
YC_API_KEY=<service account API key>
YC_FOLDER_ID=<folder identifier>
```

The file must never reach the repository — it is already in `.gitignore`.

### 3. Check

```bash
python voice_mcp.py --selftest
```

The self-test makes no network calls, does not touch the microphone and does not
utter a sound. It checks the protocol, the behaviour when credentials are
missing, and the presence of ffmpeg and a microphone.

### 4. Wiring it into a client

**Claude Code:**

```bash
claude mcp add voice -- python /path/to/voice_mcp.py
```

**Or by hand**, in the client configuration:

```json
{
  "mcpServers": {
    "voice": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/voice_mcp.py"]
    }
  }
}
```

Then restart the client — MCP servers are picked up at startup. This also means
**edits to the file do not take effect until the client restarts**. If the voice
still behaves the old way after a change, that is almost always why.

---

## Tool reference

All four tools return a single text block. Failures come back as text too, not
as protocol errors — see [Errors](#errors).

### `voice_check`

No parameters. Returns a short report: whether ffmpeg is on the path, how many
input devices were found and their names, whether the API key and folder are
set, and whether SpeechKit answered a probe request.

### `listen`

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `seconds` | integer | `8` | How long to record. Capped at `MAX_SECONDS` (60) |
| `device` | string | first device found | Microphone name as the system reports it |
| `lang` | string | `ru-RU` | Recognition language |

Returns the recognised text, or a note saying nothing was recognised.

### `speak`

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `text` | string | — | **Required.** What to say |
| `voice` | string | `filipp` | SpeechKit voice |
| `lang` | string | `ru-RU` | Synthesis language |

Returns a confirmation with the number of characters synthesised.

### `converse`

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `text` | string | — | **Required.** What to say before recording |
| `seconds` | integer | `15` | How long to listen afterwards |
| `voice` | string | `filipp` | SpeechKit voice |
| `lang` | string | `ru-RU` | Language for both halves |

Returns what the other side said. This is the tool to use for an actual
conversation.

---

## Settings

Edited at the top of the file:

| Setting | Default | Meaning |
|---|---|---|
| `DEFAULT_VOICE` | `filipp` | Synthesis voice. The full list is in the SpeechKit documentation |
| `DEFAULT_LANG` | `ru-RU` | Language for recognition and synthesis |
| `RATE` | `16000` | Sample rate, Hz |
| `MAX_SECONDS` | `60` | Hard cap on a single recording |

### About the sample rate

16 kHz is the standard for speech. You can set 48000; the difference is barely
audible while the amount of data triples. On a weak machine you will hear that
as latency, not as quality.

---

## How it works inside

### Raw audio instead of compressed

Both recording and synthesis work with uncompressed PCM. That looks wasteful,
but on weak hardware it turned out to be the only thing that worked:

- **on playback**, a compressed stream came out in stutters and turned into an
  unintelligible stream of sounds — the decoder could not keep up;
- **on recognition**, a compressed recording came back empty although the
  microphone was capturing audio correctly. Verified by measuring the recording
  level directly and then recognising the very same fragment as raw PCM, which
  worked.

So raw PCM is handed to the system as it is: synthesis gets wrapped in a WAV
header (44 bytes, built by hand) and played by the OS, and the recording goes to
SpeechKit untouched. No encoder and no decoder is left anywhere in the chain.

### The MCP protocol, written out by hand

The server parses JSON-RPC over stdio itself — about forty lines. That is
cheaper than taking on a dependency for three methods: `initialize`,
`tools/list`, `tools/call`.

### Encoding

On Windows, standard input is read in the system code page by default, so
Cyrillic arrives mangled and synthesis faithfully reads out the garbage. The
server switches both input and output to UTF-8 explicitly.

The symptom of this particular bug is distinctive: the server reports roughly
twice as many characters as the text actually has. It is counting the bytes of a
broken string. If you hear letters and numbers being read out instead of your
sentence, look here first.

### Errors

A failure is returned as a result with a note, not as a protocol error: the
agent needs to read the reason and pass it on to the human in words. The
messages are phrased so that it is clear what to fix.

---

## Privacy

**The server does not listen continuously.** Recording starts only on a `listen`
or `converse` call and lasts no longer than the given limit. A background-open
microphone is deliberately not provided.

**Key values are never printed** and never reach the log.

**Audio goes to Yandex Cloud** — it is a cloud recognition and synthesis
service. If your situation does not allow that, use a local engine; this server
is not for that case.

---

## Cost

SpeechKit is billed per second of recognition and per character of synthesis.
Current prices are on the
[pricing page](https://yandex.cloud/en/docs/speechkit/pricing). New accounts get
a starting grant.

A conversation costs little, but measure it at your own volumes before putting
voice into daily use.

---

## Troubleshooting

**"0 microphones found" although the system sees one.**
The format of ffmpeg's device listing changed between versions. The server
understands both, but if the device is still not found, check by hand:

```bash
ffmpeg -list_devices true -f dshow -i dummy
```

The device name can also be passed explicitly in the `device` parameter.

**A USB device in an error state, code 10.**
A common problem on older motherboards: the front USB ports do not deliver
enough power for a headset. Move it to a port on the back panel.

**Speech is audible, but it is not your text.**
Check the encoding — see the section above. An indirect sign: the character
count in the server's reply is twice the real one.

**The recording is empty although the microphone works.**
Make sure the headset is selected as the default input device in the system
sound settings and that the level is not at zero. If it is, and recognition is
still empty, suspect the audio format rather than the microphone.

**The voice ignores your changes to the file.**
MCP servers are started by the client. Restart the client.

---

## License

MIT. Use it.

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Голосовой MCP-сервер: агент слышит и говорит по-русски через Яндекс SpeechKit.

Даёт любому MCP-клиенту два умения — слушать микрофон и отвечать вслух, —
а также их сочетание одним действием, чтобы разговор шёл без пауз.

ПОЧЕМУ БЕЗ ЗАВИСИМОСТЕЙ
Сервер рассчитан на слабую машину: двухъядерный процессор и меньше гигабайта
свободной памяти. Поэтому ни одной сторонней библиотеки — протокол MCP разобран
вручную, HTTP идёт через стандартную библиотеку, запись и воспроизведение через
ffmpeg и winsound. Ставить нечего, кроме самого ffmpeg.

ПОЧЕМУ СЫРОЙ ЗВУК
И запись, и синтез работают с несжатым PCM. Сжатые форматы на слабом железе
давали рваную речь при воспроизведении и пустой ответ распознавания. Сырой
поток тяжелее по объёму, но не требует ни кодировщика, ни декодера — на такой
машине это решающее.

ЧТО УМЕЕТ
  voice_check  — диагностика: микрофон, ffmpeg, доступ к SpeechKit.
                 Ничего не записывает и не произносит.
  listen       — записать с микрофона и вернуть распознанный текст.
  speak        — произнести текст вслух.
  converse     — сказать фразу и сразу записать ответ одним действием.
                 Для живого разговора: без паузы между репликами.

ДОСТУП
Читается из файла yandex-speechkit.env рядом с сервером:
  YC_API_KEY=...       ключ сервисного аккаунта Яндекс Облака
  YC_FOLDER_ID=...     идентификатор каталога
Значения не печатаются и не попадают в журнал. SpeechKit — сервис Яндекс
ОБЛАКА: OAuth-токены Яндекс ID к нему не подходят, нужен отдельный сервисный
аккаунт с ролями ai.speechkit-stt.user и ai.speechkit-tts.user.

ЗАПУСК
  python voice_mcp.py              обычный режим, общение по stdio
  python voice_mcp.py --selftest   проверка без сети, микрофона и ключа
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
ENV_FILE = BASE / "secure" / "yandex-speechkit.env"

STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"

# Голос по умолчанию — женский, нейтральный. Список голосов SpeechKit меняется,
# поэтому имя берётся из настроек, а не зашивается намертво.
# Мужской голос по умолчанию. Список голосов SpeechKit меняется, поэтому
# имя берётся из настроек, а не зашивается намертво.
DEFAULT_VOICE = "filipp"

# Частота для речи. 16 кГц — стандарт для голоса: втрое меньше данных, чем
# 48 кГц, быстрее приходит и легче машине, а на слух для речи неотличимо.
# Меньше данных — быстрее отклик на слабой машине.
RATE = 16000
DEFAULT_LANG = "ru-RU"
MAX_SECONDS = 60          # длиннее одной реплики не пишем: это диалог, не диктофон

# Слово, которым человек зовёт агента, и слова, которыми разговор закрывают.
# Прощание распознаётся с обеих сторон: сказал человек «пока» — цикл
# закрывается, и агенту не нужно гадать, ждут от него ещё реплику или нет.
СЛОВО_ВЫЗОВА = "поговорим"
ПРОЩАНИЕ = ("пока", "до свидания", "конец связи", "закончили")

# «Пока» в русском чаще значит «пока что», а не прощание. Поймано на первом
# же живом разговоре: на фразе «пока что работы на ней» диалог закрылся,
# хотя человек только начал говорить. Слово прощается, лишь когда за ним не
# идёт продолжение.
НЕ_ПРОЩАНИЕ_ПОСЛЕ = ("что", "не", "нет", "еще", "только", "лишь", "рано",
                     "буду", "будем", "буквально")

# Средняя амплитуда 16-битного отсчёта, ниже которой считаем, что молчат.
# Нужна только в режиме ожидания: распознавание платное, а ждущий микрофон
# почти всё время пишет пустую комнату.
ТИШИНА = 300


def load_env() -> dict:
    """Ключи из файла. Отсутствие файла — не падение, а честный ответ «не настроено»."""
    out = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for k in ("YC_API_KEY", "YC_FOLDER_ID"):
        if not out.get(k) and os.environ.get(k):
            out[k] = os.environ[k]
    return out


ЗНАКИ = re.compile(r"[^\w\s]+", re.UNICODE)


def слова_из(текст: str) -> list:
    """Речь в сравнимый вид: без регистра, без знаков, ё приравнена к е."""
    return ЗНАКИ.sub(" ", (текст or "").lower().replace("ё", "е")).split()


def есть_слово(текст: str, образцы) -> bool:
    """Совпадение целым словом, а не куском строки.

    Иначе «покажи» читается как «пока» и разговор обрывается на полуслове.
    Образец может быть из нескольких слов — «до свидания» ищется подряд.
    """
    слышно = слова_из(текст)
    for образец in образцы:
        ц = слова_из(образец)
        if ц and any(слышно[i:i + len(ц)] == ц
                     for i in range(len(слышно) - len(ц) + 1)):
            return True
    return False


def это_прощание(текст: str) -> bool:
    """Прощание ли это, а не «пока что» в середине мысли."""
    слышно = слова_из(текст)
    for образец in ПРОЩАНИЕ:
        ц = слова_из(образец)
        if not ц:
            continue
        for i in range(len(слышно) - len(ц) + 1):
            if слышно[i:i + len(ц)] != ц:
                continue
            хвост = слышно[i + len(ц):]
            if ц == ["пока"] and хвост and хвост[0] in НЕ_ПРОЩАНИЕ_ПОСЛЕ:
                continue
            return True
    return False


def громкость(pcm: bytes) -> float:
    """Средняя амплитуда моно-PCM. Запись идёт в s16le, порядок байт родной."""
    from array import array
    куски = array("h")
    try:
        куски.frombytes(pcm[:len(pcm) // 2 * 2])
    except Exception:                                    # noqa: BLE001
        return 0.0
    return (sum(abs(x) for x in куски) / len(куски)) if куски else 0.0


def тихо(pcm: bytes, порог: float = ТИШИНА) -> bool:
    return громкость(pcm) < порог


def ffmpeg_ok() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
        return True
    except Exception:                                    # noqa: BLE001
        return False


def input_devices() -> list[str]:
    """Микрофоны, которые видит Windows через ffmpeg.

    ffmpeg печатает список в stderr и завершается с ненулевым кодом — это его
    штатное поведение для dshow, а не ошибка.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace")
    except Exception:                                    # noqa: BLE001
        return []
    text = r.stderr or ""
    # ffmpeg 9 печатает каждое устройство строкой вида: "Имя" (audio).
    # Старые версии сначала выводили заголовок «DirectShow audio devices», а
    # под ним имена без пометки типа — поэтому держим оба разбора.
    names = re.findall(r'"([^"]+)"\s*\(audio\)', text)
    if names:
        return names
    audio = False
    for line in text.splitlines():
        if "DirectShow audio devices" in line:
            audio = True
            continue
        if "DirectShow video devices" in line:
            audio = False
            continue
        if audio and '"' in line and "Alternative name" not in line:
            names.append(line.split('"')[1])
    return names


def record(seconds: int, device: str | None) -> bytes:
    """Записать с микрофона в OggOpus — формат, который SpeechKit принимает как есть."""
    devices = input_devices()
    if not devices:
        raise RuntimeError(
            "микрофон не найден. На этой машине аудиоустройств нет вовсе — "
            "воткните USB-гарнитуру, она принесёт свою звуковую карту")
    name = device or devices[0]
    # Пишем сырой PCM, а не сжатый поток: на сжатом распознавание возвращало
    # пустоту, хотя микрофон исправно писал Проверено прямым замером уровня записи
    # и пробным распознаванием того же фрагмента.
    out = Path(tempfile.gettempdir()) / "voice_mcp_in.pcm"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "dshow", "-i", "audio=%s" % name,
           "-t", str(max(1, min(seconds, MAX_SECONDS))),
           "-ac", "1", "-ar", str(RATE), "-f", "s16le", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_SECONDS + 30,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out.exists():
        raise RuntimeError("запись не удалась: %s" % (r.stderr or "").strip()[:300])
    return out.read_bytes()


def wav_header(pcm_len: int, rate: int = RATE, channels: int = 1,
               bits: int = 16) -> bytes:
    """Заголовок WAV поверх сырого PCM. Нужен, чтобы отдать звук системе без
    единого декодера: на этой машине два ядра 2011 года, и разбор сжатого
    потока давал заикание и кашу вместо речи."""
    import struct
    byte_rate = rate * channels * bits // 8
    block = channels * bits // 8
    return (b"RIFF" + struct.pack("<I", 36 + pcm_len) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, block, bits) +
            b"data" + struct.pack("<I", pcm_len))


def play(audio: bytes) -> None:
    """Воспроизвести сырой PCM средствами Windows, без декодеров.

    winsound — часть стандартной библиотеки и отдаёт звук прямо системе.
    ffplay оставлен запасным путём на случай, если winsound недоступен.
    """
    tmp = Path(tempfile.gettempdir()) / "voice_mcp_out.wav"
    tmp.write_bytes(wav_header(len(audio)) + audio)
    try:
        import winsound
        winsound.PlaySound(str(tmp), winsound.SND_FILENAME)
        return
    except Exception:                                    # noqa: BLE001
        pass
    subprocess.run(["ffplay", "-autoexit", "-nodisp", "-hide_banner",
                    "-loglevel", "error", str(tmp)],
                   capture_output=True, timeout=180)


def http(url: str, data: bytes, headers: dict, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def speechkit_stt(audio: bytes, env: dict, lang: str = DEFAULT_LANG) -> str:
    q = urllib.parse.urlencode({"lang": lang, "folderId": env["YC_FOLDER_ID"],
                                "format": "lpcm", "sampleRateHertz": str(RATE)})
    body = http(STT_URL + "?" + q, audio,
                {"Authorization": "Api-Key " + env["YC_API_KEY"]})
    return (json.loads(body.decode("utf-8")).get("result") or "").strip()


def speechkit_tts(text: str, env: dict, voice: str = DEFAULT_VOICE,
                  lang: str = DEFAULT_LANG) -> bytes:
    # Просим сырой PCM, а не сжатый поток: его можно отдать системе как есть.
    data = urllib.parse.urlencode({
        "text": text, "lang": lang, "voice": voice,
        "format": "lpcm", "sampleRateHertz": str(RATE),
        "folderId": env["YC_FOLDER_ID"]}).encode("utf-8")
    return http(TTS_URL, data, {
        "Authorization": "Api-Key " + env["YC_API_KEY"],
        "Content-Type": "application/x-www-form-urlencoded"})


def human_error(e: Exception) -> str:
    """Сообщение, по которому понятно, что чинить, без значений ключей."""
    if isinstance(e, KeyError):
        return ("не настроен доступ к SpeechKit. Нужен файл %s с полями "
                "YC_API_KEY и YC_FOLDER_ID. SpeechKit — сервис Яндекс ОБЛАКА, "
                "наши токены Директа и Метрики сюда не подходят." % ENV_FILE)
    if isinstance(e, urllib.error.HTTPError):
        code = e.code
        if code in (401, 403):
            return "SpeechKit отказал (%d): ключ или каталог неверны, либо у сервисного аккаунта нет ролей на распознавание и синтез" % code
        if code == 429:
            return "SpeechKit ограничил частоту (429): слишком много обращений подряд"
        return "SpeechKit ответил %d" % code
    if isinstance(e, urllib.error.URLError):
        return "сеть недоступна: %s" % e.reason
    return str(e)


# ----------------------------------------------------------------- инструменты

def tool_voice_check(_args: dict) -> str:
    env = load_env()
    devices = input_devices()
    lines = [
        "ffmpeg: %s" % ("есть" if ffmpeg_ok() else "НЕТ — без него ни записи, ни воспроизведения"),
        "микрофонов найдено: %d%s" % (len(devices), (" — " + ", ".join(devices[:3])) if devices else ""),
        "ключ SpeechKit: %s" % ("задан" if env.get("YC_API_KEY") else "НЕ ЗАДАН"),
        "каталог облака: %s" % ("задан" if env.get("YC_FOLDER_ID") else "НЕ ЗАДАН"),
    ]
    if env.get("YC_API_KEY") and env.get("YC_FOLDER_ID"):
        try:
            speechkit_tts("проверка связи", env)
            lines.append("ответ SpeechKit: синтез работает")
        except Exception as e:                           # noqa: BLE001
            lines.append("ответ SpeechKit: %s" % human_error(e))
    else:
        lines.append("ответ SpeechKit: не проверял, нет доступа")
    if not devices:
        lines.append("Что делать: воткнуть USB-гарнитуру — она принесёт свою звуковую карту.")
    return "\n".join(lines)


def bell(start: bool) -> None:
    """Колокольчик на границах записи: высокий — начало, низкий — конец.

    Без него не понять, когда говорить: пока человек ждёт сигнала, окно уже
    закрывается, и запись выходит пустой.
    """
    try:
        import winsound
        winsound.Beep(880 if start else 440, 150)
    except Exception:                                    # noqa: BLE001
        pass


def tool_wake(args: dict) -> str:
    """Ждать, пока человек позовёт голосом.

    Пишем короткими кусками и смотрим громкость на месте: в облако уходит
    только то, где кто-то говорил. Колокольчик звучит один раз на входе и
    один раз на выходе — человек должен знать, что микрофон открыт, но
    сигнал каждые пять секунд был бы невыносим.
    """
    env = load_env()
    if not env.get("YC_API_KEY") or not env.get("YC_FOLDER_ID"):
        raise KeyError("speechkit")
    слово = args.get("wake") or СЛОВО_ВЫЗОВА
    окно = max(3, min(int(args.get("window") or 5), 15))
    предел = time.time() + max(1.0, float(args.get("minutes") or 10)) * 60
    язык = args.get("lang") or DEFAULT_LANG
    устройство = args.get("device")
    распознано = 0
    bell(True)
    try:
        while time.time() < предел:
            кусок = record(окно, устройство)
            if тихо(кусок):
                continue
            распознано += 1
            услышано = speechkit_stt(кусок, env, язык)
            if есть_слово(услышано, (слово,)):
                return "вызов: %s" % (услышано.strip() or слово)
    finally:
        bell(False)
    return ("за отведённое время обращения не было "
            "(кусков с речью распознано: %d)" % распознано)


def tool_listen(args: dict) -> str:
    env = load_env()
    if not env.get("YC_API_KEY") or not env.get("YC_FOLDER_ID"):
        raise KeyError("speechkit")
    seconds = int(args.get("seconds") or 8)
    bell(True)
    audio = record(seconds, args.get("device"))
    bell(False)
    text = speechkit_stt(audio, env, args.get("lang") or DEFAULT_LANG)
    return text or "(тишина — ничего не распознано)"


def tool_speak(args: dict) -> str:
    env = load_env()
    if not env.get("YC_API_KEY") or not env.get("YC_FOLDER_ID"):
        raise KeyError("speechkit")
    text = (args.get("text") or "").strip()
    if not text:
        return "нечего произносить: текст пуст"
    audio = speechkit_tts(text, env, args.get("voice") or DEFAULT_VOICE,
                          args.get("lang") or DEFAULT_LANG)
    play(audio)
    return "произнесено, символов: %d" % len(text)


def tool_converse(args: dict) -> str:
    """Сказать и сразу слушать — одним действием.

    Зачем отдельный инструмент. Если звать speak и listen по очереди, между
    ними успевает пройти мой круг размышления, и человек ждёт сигнала секунд
    пятнадцать после реплики агента. Здесь речь, колокольчик и
    запись идут подряд внутри одного вызова, без пауз.
    """
    env = load_env()
    if not env.get("YC_API_KEY") or not env.get("YC_FOLDER_ID"):
        raise KeyError("speechkit")
    text = (args.get("text") or "").strip()
    if text:
        audio = speechkit_tts(text, env, args.get("voice") or DEFAULT_VOICE,
                              args.get("lang") or DEFAULT_LANG)
        play(audio)
    seconds = int(args.get("seconds") or 15)
    bell(True)
    heard = record(seconds, args.get("device"))
    bell(False)
    said = speechkit_stt(heard, env, args.get("lang") or DEFAULT_LANG)
    if not said:
        return "(тишина — ничего не распознано)"
    # Явная отметка, а не догадка по тексту: без неё агент не знает,
    # ждут от него следующую реплику или разговор закончен.
    return said + ("\n[прощание — разговор закрыт]" if это_прощание(said) else "")


TOOLS = [
    {"name": "voice_check",
     "description": "Проверить голосовой контур: микрофон, ffmpeg, доступ к SpeechKit. "
                    "Ничего не записывает и не произносит.",
     "inputSchema": {"type": "object", "properties": {}},
     "handler": tool_voice_check},
    {"name": "wake",
     "description": "Ждать, пока человек позовёт голосом кодовым словом "
                    "(по умолчанию «поговорим»), и вернуть услышанное. "
                    "Микрофон на это время открыт; тишина в облако не уходит.",
     "inputSchema": {"type": "object", "properties": {
         "wake": {"type": "string", "description": "кодовое слово, по умолчанию «поговорим»"},
         "minutes": {"type": "number", "description": "сколько минут ждать, по умолчанию 10"},
         "window": {"type": "integer", "description": "длина куска записи в секундах, 3–15, по умолчанию 5"},
         "device": {"type": "string", "description": "имя микрофона"},
         "lang": {"type": "string", "description": "язык, по умолчанию ru-RU"}}},
     "handler": tool_wake},
    {"name": "listen",
     "description": "Записать речь с микрофона и вернуть распознанный текст (русский).",
     "inputSchema": {"type": "object", "properties": {
         "seconds": {"type": "integer", "description": "сколько секунд писать, по умолчанию 8, не больше 60"},
         "device": {"type": "string", "description": "имя микрофона, по умолчанию первый найденный"},
         "lang": {"type": "string", "description": "язык, по умолчанию ru-RU"}}},
     "handler": tool_listen},
    {"name": "speak",
     "description": "Произнести текст вслух по-русски через SpeechKit.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "что сказать"},
         "voice": {"type": "string", "description": "голос SpeechKit, по умолчанию filipp"},
         "lang": {"type": "string", "description": "язык, по умолчанию ru-RU"}},
         "required": ["text"]},
     "handler": tool_speak},
    {"name": "converse",
     "description": "Сказать фразу и сразу же, без паузы, записать ответ. "
                    "Для живого разговора: речь, сигнал, запись и распознавание "
                    "идут одним действием.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "что сказать перед записью"},
         "seconds": {"type": "integer", "description": "сколько слушать, по умолчанию 15"},
         "voice": {"type": "string", "description": "голос, по умолчанию filipp"},
         "lang": {"type": "string", "description": "язык, по умолчанию ru-RU"}},
         "required": ["text"]},
     "handler": tool_converse},
]


# -------------------------------------------------------------- протокол MCP

def handle(msg: dict) -> dict | None:
    """Один запрос JSON-RPC. None — уведомление, отвечать не нужно."""
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "voice-speechkit", "version": "1.0.0"}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if not tool:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": "нет инструмента %s" % name}}
        try:
            text = tool["handler"](params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as e:                           # noqa: BLE001
            # Ошибку отдаём как результат с пометкой, а не как сбой протокола:
            # агенту нужно прочитать причину и сказать её человеку.
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "не получилось: " + human_error(e)}],
                "isError": True}}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "неизвестный метод %s" % method}}


def serve() -> int:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def selftest() -> int:
    """Проверка без сети, микрофона и ключа — чтобы собирать сервер до их появления."""
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and cond
        print("  %-52s %s%s" % (name, "ок" if cond else "ПРОВАЛ", (" — " + detail) if detail else ""))

    print("Протокол:")
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    check("initialize отвечает именем сервера", r["result"]["serverInfo"]["name"] == "voice-speechkit")
    check("уведомление не требует ответа", handle({"method": "notifications/initialized"}) is None)
    r = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in r["result"]["tools"]]
    check("пять инструментов объявлены",
          names == ["voice_check", "wake", "listen", "speak", "converse"], ", ".join(names))
    check("в описании инструментов нет обработчиков",
          all("handler" not in t for t in r["result"]["tools"]))
    r = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "нет-такого", "arguments": {}}})
    check("неизвестный инструмент даёт ошибку протокола", "error" in r)

    print("Поведение без настроенного доступа:")
    # Проверяем ветку «не настроено» на заведомо пустом пути. Иначе, когда ключ
    # уже задан, самотест не только провалится, но и ЗАГОВОРИТ вслух — проверка
    # не должна иметь побочных действий Проверка не должна иметь побочных действий.
    global ENV_FILE
    real_env = ENV_FILE
    ENV_FILE = Path(tempfile.gettempdir()) / "voice_mcp_no_such.env"
    r = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "speak", "arguments": {"text": "привет"}}})
    txt = r["result"]["content"][0]["text"]
    check("speak объясняет, что не настроено", r["result"].get("isError") and "SpeechKit" in txt)
    check("в сообщении названо ОБЛАКО, а не Паспорт", "Облак" in txt or "ОБЛАК" in txt)
    r = handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "listen", "arguments": {}}})
    check("listen тоже не падает молча", r["result"].get("isError"))
    ENV_FILE = real_env
    check("настоящий доступ не тронут", ENV_FILE == real_env)

    print("Разбор услышанного:")
    check("кодовое слово узнаётся", есть_слово("Ну что, поговорим?", (СЛОВО_ВЫЗОВА,)))
    check("слово узнаётся в другом регистре и с ё", есть_слово("ПОГОВОРИМ", (СЛОВО_ВЫЗОВА,)))
    check("прощание узнаётся", это_прощание("Ладно, пока"))
    check("прощание из двух слов узнаётся", это_прощание("До свидания!"))
    # Главная ловушка: «пока» внутри другого слова не должно закрывать разговор.
    check("«покажи» не читается как «пока»", not это_прощание("покажи остатки"))
    # Поймано вживую 07.10.2026: «пока что» закрыло разговор на полуслове.
    check("«пока что» не закрывает разговор",
          not это_прощание("пока что работы на ней не начнём"))
    check("«пока не» не закрывает разговор", not это_прощание("пока не надо"))
    check("«пока» в конце фразы прощается", это_прощание("ну всё, пока"))
    check("«ладно пока до завтра» прощается", это_прощание("ладно пока до завтра"))
    check("«показатели» не закрывают разговор", not это_прощание("дай показатели за неделю"))
    check("чужое слово не будит", not есть_слово("погода сегодня", (СЛОВО_ВЫЗОВА,)))
    check("пустая речь не будит", not есть_слово("", (СЛОВО_ВЫЗОВА,)))
    import struct as _s
    тишина = _s.pack("<8h", *([0] * 8))
    речь = _s.pack("<8h", *([9000, -9000] * 4))
    check("тишина распознаётся как тишина", тихо(тишина))
    check("речь тишиной не считается", not тихо(речь))
    check("обрезанный байт не роняет расчёт", громкость(b"") == 0.0)

    print("Среда этой машины:")
    check("ffmpeg доступен", ffmpeg_ok())
    devices = input_devices()
    print("  %-52s %d" % ("микрофонов найдено", len(devices)))
    if not devices:
        print("     гарнитуры ещё нет — это ожидаемо, записи не будет до неё")
    print()
    print("Итог:", "всё зелёное" if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


if __name__ == "__main__":
    # ВХОДЯЩИЙ поток тоже обязан быть UTF-8. На Windows по умолчанию он
    # читается кириллической кодировкой системы, и русский текст запроса
    # приходит искажённым — SpeechKit честно озвучивает мусор, получаются
    # «буквы и номера» вместо речи. Через командную строку это не всплывало:
    # там кодировку задавала переменная окружения Проверка не должна иметь побочных действий.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(selftest() if "--selftest" in sys.argv else serve())

# -*- coding: utf-8 -*-
"""
Jarvis Bridge — голосовой мост между микрофоном и окном MultiTool.
Схема: wake-word "Джарвис" -> STT (Vosk) -> ввод команды в окно MultiTool
       -> OCR ответа с экрана (Windows.Media.Ocr) -> TTS озвучка (edge_tts).
"""
import asyncio
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

import numpy as np
import sounddevice as sd
import pyautogui
import pygetwindow as gw
import pyperclip
from vosk import Model, KaldiRecognizer

import sys as _sys
# В PyInstaller (one-file) __file__ лежит во временном _MEI-каталоге.
# ASSET_DIR — времянка со встроенными ресурсами (модель Vosk, аватар).
# RUNTIME_DIR — стабильный каталог для данных на диске (state/speech/заметки).
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    ASSET_DIR = sys._MEIPASS
    RUNTIME_DIR = os.path.dirname(sys.executable)
    MODEL_DIR = os.path.join(ASSET_DIR, 'vosk-model-small-ru-0.22')
    SPK_MODEL_DIR = os.path.join(ASSET_DIR, 'vosk-model-spk-0.4')
else:
    ASSET_DIR = os.path.dirname(os.path.abspath(__file__))
    RUNTIME_DIR = ASSET_DIR
    MODEL_DIR = r"C:\Users\DK_ART\AppData\Local\Temp\gigatool\vosk-model\vosk-model-small-ru-0.22"
    SPK_MODEL_DIR = r"C:\Users\DK_ART\AppData\Local\Temp\gigatool\vosk-model-spk-0.4"
SR = 16000
BLOCK = 4000
WAKE_WORDS = ("джарвис", "джарвис", "jarvis", "жарвис", "джарвис ай",
              "джарвис слушай", "джарвис послушай")
VOICE = "ru-RU-DmitryNeural"
MULTITOOL_WINDOW = "MultiTool"
BASE_DIR = RUNTIME_DIR
STATE_FILE = os.path.join(BASE_DIR, "jarvis_state.json")
MIC_DEVICE = None
for i, a in enumerate(sys.argv):
    if a == "--mic" and i + 1 < len(sys.argv):
        try:
            MIC_DEVICE = int(sys.argv[i + 1])
        except ValueError:
            MIC_DEVICE = None
if MIC_DEVICE is not None:
    log(f"[audio] override mic device -> {MIC_DEVICE}")

TTS_PLAY_EXE = None
for cand in (
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
):
    if os.path.exists(cand):
        TTS_PLAY_EXE = cand
        break

IS_WINDOWS = sys.platform.startswith("win")

LOG_FILE = os.path.join(RUNTIME_DIR, "jarvis.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def set_state(state: str):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"state": state, "ts": time.time()}, f)
    except Exception as e:
        log(f"[state] write error: {e}")


SPEECH_FILE = os.path.join(BASE_DIR, "jarvis_speech.json")


def set_speech(text: str):
    try:
        with open(SPEECH_FILE, "w", encoding="utf-8") as f:
            json.dump({"text": text, "ts": time.time()}, f, ensure_ascii=False)
    except Exception as e:
        log(f"[speech] write error: {e}")


def on_quit_cue():
    return False


class JarvisBridge:
    def __init__(self):
        self.model = Model(MODEL_DIR)
        # Распознавание говорящего (spk). Модель опциональна — если её нет,
        # ассистент работает без идентификации (graceful degradation).
        self.spk = None
        spk_exist = os.path.isdir(SPK_MODEL_DIR)
        if spk_exist:
            try:
                self.spk = SpkModel(SPK_MODEL_DIR)
                log("[spk] модель говорящего загружена")
            except Exception as e:
                self.spk = None
                log(f"[spk] ошибка загрузки модели: {e}")
        else:
            log("[spk] модель говорящего не найдена, идентификация отключена")
        if self.spk is not None:
            self.rec = KaldiRecognizer(self.model, SR, self.spk)
        else:
            self.rec = KaldiRecognizer(self.model, SR)
        self.rec.SetWords(True)
        self.audio_q = queue.Queue()
        self.wake_detected = False
        self.speaking_lock = False
        self.wake_lock_until = 0.0
        self.capture_dev = None
        self.last_spk = None          # эмбеддинг голоса последней фразы
        self.speakers = self._load_speakers()

    # ---------- распознавание говорящего ----------
    @staticmethod
    def _extract_spk(result_json) -> list:
        try:
            v = result_json.get("spk")
            return list(v) if v else []
        except Exception:
            return []

    def _load_speakers(self) -> dict:
        path = os.path.join(RUNTIME_DIR, "speakers.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            log(f"[spk] load speakers error: {e}")
        return {}

    def _save_speakers(self):
        path = os.path.join(RUNTIME_DIR, "speakers.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.speakers, f, ensure_ascii=False)
        except Exception as e:
            log(f"[spk] save speakers error: {e}")

    @staticmethod
    def _cosine(a: list, b: list) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        import math
        ab = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return ab / (na * nb)

    def identify_speaker(self) -> str:
        """Возвращает имя говорящего или '' если не распознан / недостаточно данных."""
        if not self.spk or not self.last_spk:
            return ""
        best_name, best_cos, threshold = "", 0.0, 0.55
        for name, vec in self.speakers.items():
            if len(vec) != len(self.last_spk):
                continue
            c = self._cosine(self.last_spk, vec)
            if c > best_cos:
                best_cos, best_name = c, name
        return best_name if best_cos >= threshold else ""

    def _announce_speaker(self):
        name = self.identify_speaker()
        if name:
            self._say(f"Говорит, {name}")
            return True
        return False

    def _register_voice(self, text: str) -> bool:
        m = re.search(r"(?:как|это|зовут|имя|меня зовут)\s+([а-яё]+)", text, re.I)
        if m:
            name = m.group(1).capitalize()
        else:
            words = re.findall(r"[а-яё]+", text.lower())
            known = {"запомни", "голос", "это", "меня", "зовут", "как", "имя", "джарвис"}
            cand = [w for w in words if w not in known]
            name = cand[-1].capitalize() if cand else ""
        if not name:
            self._say("Как назвать этот голос? Скажите: запомни голос как Алёна")
            return True
        if self.spk is None:
            self._say("Распознавание голоса отключено — модель говорящего не найдена")
            return True
        if not self.last_spk:
            self._say("Не удалось снять отпечаток голоса из этой фразы. Повторите, пожалуйста, ещё раз")
            return True
        self.speakers[name] = self.last_spk
        self._save_speakers()
        self._say(f"Запомнил голос — {name}")
        log(f"[spk] зарегистрирован голос: {name} (dim={len(self.last_spk)})")
        return True

    def _wake_audio_path(self):
        return os.path.join(tempfile.gettempdir(), "jarvis_wake.wav")

    def _preload_wake(self):
        try:
            import edge_tts
            mp3 = os.path.join(tempfile.gettempdir(), "jarvis_wake_tmp.mp3")
            wav = self._wake_audio_path()
            async def _gen():
                for rate in ("+40%", "+30%", None):
                    try:
                        comm = edge_tts.Communicate(
                            "Слушаю, мастер", VOICE, rate=rate)
                        await comm.save(mp3)
                        return
                    except Exception:
                        await asyncio.sleep(1.0)
                raise RuntimeError("edge_tts не ответил")
            asyncio.run(_gen())
            ff = None
            for cand in (
                r"C:\Users\DK_ART\AppData\Roaming\Python\Python312\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe",
                r"C:\Users\DK_ART\AppData\Roaming\Python\Python312\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe",
            ):
                if os.path.exists(cand):
                    ff = cand
                    break
            if ff:
                subprocess.run(
                    [ff, "-y", "-i", mp3, "-ar", "44100", "-ac", "2", wav],
                    capture_output=True)
            else:
                shutil.copy(mp3, wav)
            log("[preload] wake audio готов (wav)")
        except Exception as e:
            log(f"[preload] wake audio не готов: {e}")

    def _wake_respond(self):
        path = self._wake_audio_path()
        if os.path.exists(path):
            self.wake_lock_until = time.time() + 2.2
            threading.Thread(
                target=self._play_mp3_blocking, args=(path,), daemon=True).start()
        else:
            asyncio.run_coroutine_threadsafe(self.speak("Слушаю, мастер"), self.loop)

    # ---------- audio capture ----------
    def audio_callback(self, indata, frames, t, status):
        self.audio_q.put(indata.copy())

    def run_capture(self, stop_event):
        def cb(indata, frames, t, status):
            self.audio_q.put(indata.copy())
        try:
            with sd.InputStream(
                samplerate=SR,
                blocksize=BLOCK,
                device=self.capture_dev or None,
                channels=1,
                dtype="int16",
                callback=cb,
            ):
                while not stop_event.is_set():
                    time.sleep(0.1)
        except Exception as e:
            log(f"[capture] error: {e}")

    ECHO_PHRASES = ("слушаю мастер", "слушаю", "мастер", "слушаю мастера",
                "слушаю мой мастер", "джарвис слушаю", "готов к работе",
                "слушает", "слушает мастер", "слушает магистр", "слушает магистра",
                "слушаю магистра", "слушает мой мастер", "слушает мастера",
                "слушаю мой", "слушает мой", "джавис слушает")

    # ---------- streaming recognition with wake word ----------
    def recognize_stream(self, stop_event):
        buf = b""
        cmd_buf = ""
        last_speech_ts = time.time()      # время последнего голоса (VAD)
        wake_ts = 0.0                     # когда проснулись
        SILENCE_TIMEOUT = 1.3             # сек тишины => фраза закончилась
        MAX_LISTEN = 6.0                  # макс. времени на команду
        ENERGY = 400.0                    # порог VAD (RMS)

        def finalize(final_text: str):
            if final_text:
                self.process_command(final_text)
            return ""

        while not stop_event.is_set():
            try:
                data = self.audio_q.get(timeout=0.2)
            except queue.Empty:
                if self.wake_detected and cmd_buf:
                    # пользователь замолчал — завершаем фразу
                    if (time.time() - last_speech_ts) > SILENCE_TIMEOUT or \
                       (time.time() - wake_ts) > MAX_LISTEN:
                        log("[end] тишина — завершаю команду")
                        final_text = self._finalize_text() or cmd_buf
                        cmd_buf = finalize(final_text)
                        self.wake_detected = False
                continue
            if self.speaking_lock or time.time() < self.wake_lock_until:
                buf = b""
                continue
            # VAD: считаем RMS энергии фрагмента
            arr = np.frombuffer(data.tobytes(), dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr ** 2))) if arr.size else 0.0
            if rms > ENERGY:
                last_speech_ts = time.time()
            raw = data.tobytes()
            buf += raw
            if len(buf) < SR // 4:
                continue
            accepted = self.rec.AcceptWaveform(buf)
            buf = b""
            if accepted:
                r = json.loads(self.rec.Result())
                text = r.get("text", "").strip()
                self.last_spk = self._extract_spk(r)
                if self.wake_detected:
                    # целая фраза распознана — это и есть команда
                    if text and len(text) >= len(cmd_buf):
                        log(f"[cmd-final] {text}")
                        self.process_command(text)
                        cmd_buf = ""
                        self.wake_detected = False
                else:
                    self.handle_text(text)
            else:
                partial = json.loads(self.rec.PartialResult()).get("partial", "").strip()
                if partial:
                    pl = partial.lower()
                    if not self.wake_detected:
                        if any(w in pl for w in WAKE_WORDS):
                            self.wake_detected = True
                            log(">>> ДЖАРВИС ПРОСНУЛСЯ")
                            self._wake_respond()
                            cmd_buf = ""
                            wake_ts = time.time()
                            last_speech_ts = time.time()
                    else:
                        if any(e in pl for e in self.ECHO_PHRASES) and len(pl) < 20:
                            log(f"[echo-ignored] {partial}")
                            continue
                        cmd_buf = partial
                        last_speech_ts = time.time()
                        wake_ts = time.time()
                        log(f"[hear] {partial}")
        return

    def _finalize_text(self) -> str:
        try:
            r = json.loads(self.rec.FinalResult())
            self.last_spk = self._extract_spk(r)
            return r.get("text", "").strip()
        except Exception:
            return ""

    def handle_text(self, text: str):
        t = text.lower()
        if not self.wake_detected:
            if any(w in t for w in WAKE_WORDS):
                self.wake_detected = True
                log(">>> ДЖАРВИС ПРОСНУЛСЯ")
                self._wake_respond()
        else:
            self.process_command(text)

    def process_command(self, text: str):
        log(f"[cmd] {text}")
        set_state("speaking")
        self.wake_detected = False
        t = text.lower().strip()
        for w in ("джарвис", "jarvis", "жарвис", "джавис", "джарис"):
            t = t.replace(w, " ").strip()
        for e in self.ECHO_PHRASES:
            t = t.replace(e, " ").strip()
        t = re.sub(r"\s+", " ", t).strip(" .,;:-")
        if not t:
            return
        if self.spk is not None:
            who = self.identify_speaker()
            if who:
                log(f"[spk] говорит: {who}")
                self._say(f"Говорит, {who}")
            else:
                log("[spk] говорящий не распознан")
        if any(k in t for k in ("стоп", "хватит", "замолчи", "выйди", "спать")):
            self._say("Отключаюсь, мастер")
            return
        if self.handle_local(t):
            return
        set_state("multitool")
        threading.Thread(target=self.route_to_multitool, args=(t,), daemon=True).start()

    # ---------- local skills (no MultiTool) ----------
    def handle_local(self, text: str) -> bool:
        t = text.lower().strip()
        t_clean = re.sub(r"[^\w\s\-+*/.%]", " ", t)
        t_clean = re.sub(r"\s+", " ", t_clean).strip()

        # Регистрация голоса: «запомни голос как Алёна» / «это Алёна» / «меня зовут Алёна»
        if ("голос" in t or "зовут" in t) and any(k in t for k in ("запомни", "это", "меня")):
            return self._register_voice(text)

        if any(k in t for k in ("который час", "сколько времени", "который сейчас час",
                                "какое время", "сколько время")):
            from datetime import datetime
            now = datetime.now().strftime("%H:%M")
            self._say(f"Сейчас {now}")
            return True

        if any(k in t for k in ("какое сегодня число", "какой сегодня день",
                                "какое число", "сегодняшняя дата")):
            from datetime import datetime
            months = {
                1: "января", 2: "февраля", 3: "марта", 4: "апреля",
                5: "мая", 6: "июня", 7: "июля", 8: "августа",
                9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
            }
            now = datetime.now()
            self._say(f"Сегодня {now.day} {months[now.month]} {now.year} года")
            return True

        if "браузер" in t and any(k in t for k in ("открой", "запусти", "включи", "откройте")):
            self._open_browser()
            return True

        if "сайт" in t or "открой ютуб" in t or "открой ютьюб" in t or any(k in t for k in self.SITE_ALIASES):
            site = self._extract_site(text)
            if site:
                self._open_site(site)
                return True

        if any(k in t for k in ("включи экран", "включи монитор", "разбуди экран",
                                "проснись экран", "включи подсветку")):
            self._screen_on()
            return True

        if any(k in t for k in ("выключи экран", "погаси экран", "выключи монитор",
                                "усни экран", "отключи монитор", "отключи экран",
                                "потуши экран")):
            self._screen_off()
            return True

        if any(k in t for k in ("выключи компьютер", "выключи комп", "выключить компьютер",
                                "выключи пк", "отключи компьютер")):
            self._shutdown()
            return True

        if any(k in t for k in ("перезагрузи компьютер", "перезагрузи комп", "перезагрузить компьютер",
                                "перезагрузка")):
            self._restart()
            return True

        if "громкость" in t or "громче" in t or "тише" in t:
            if self._volume(text):
                return True

        if any(k in t for k in ("найди файл", "найти файл", "поиск файла",
                                "найди документ", "где лежит", "поиск на компьютере",
                                "найди на компьютере")):
            if self._find_file(text):
                return True

        if any(k in t for k in ("анекдот", "шутку", "расскажи шутку",
                                "рассмеши меня")):
            self._tell_joke()
            return True

        if any(k in t for k in ("сказку", "сказка", "расскажи сказку")):
            self._tell_fairy_tale()
            return True

        if "погода" in t or "погоду" in t:
            if self._weather(text):
                return True

        if any(k in t for k in ("мои документы", "мой компьютер", "этот компьютер",
                                "мои компьютеры", "загрузки", "скачанные",
                                "рабочий стол", "корзину", "корзина",
                                "открой папку", "откройте папку", "открыть папку",
                                "открой документы", "открой загрузки",
                                "открой картинки", "открой изображения",
                                "открой музыку", "открой видео", "открой фильмы",
                                "открой рабочий стол")):
            if self._open_folder(text):
                return True

        if any(k in t for k in ("открой", "запусти", "включи", "откройте", "запустите")):
            app = self._find_app(t)
            if app:
                self._launch_app(app)
                return True
            self._say("Не нашёл такую программу. Скажите, например, открой блокнот")
            return True

        if any(k in t for k in ("запиши", "запомни", "заметка", "запишите")):
            note = self._extract_note(text)
            if note:
                self._save_note(note)
                return True

        if "сколько будет" in t:
            expr = t_clean.replace("сколько будет", "").strip()
            if self._safe_calc(expr):
                return True

        if any(k in t for k in ("кто ты", "что ты умеешь", "твои возможности", "что ты можешь")):
            self._say("Я голосовой помощник Джарвис. Умею называть время и дату, открывать браузер и программы, записывать заметки и считать. Остальное передаю в мозг — мультитул")
            return True

        return False

    def _say(self, text: str):
        if re.search(r'[\u4e00-\u9fff]', text):
            log('[filter] китаиские символы подавлены')
            return
        asyncio.run_coroutine_threadsafe(self.speak(text), self.loop)

    def _open_browser(self):
        for cand in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ):
            if os.path.exists(cand):
                subprocess.Popen([cand], creationflags=subprocess.CREATE_NO_WINDOW)
                self._say("Открываю браузер")
                log("[skill] браузер открыт")
                return
        os.startfile("https://ya.ru")
        self._say("Открываю браузер")
        log("[skill] браузер открыт (default)")

    SHELL_FOLDERS = {
        # тип 'known' — реальная файловая папка (открывается по пути, без explorer)
        # тип 'virtual' — виртуальная (открывается через explorer.exe shell:...)
        "мои документы": ("known", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "документы"),
        "документы": ("known", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "документы"),
        "мой компьютер": ("virtual", "MyComputerFolder", "мой компьютер"),
        "этот компьютер": ("virtual", "MyComputerFolder", "мой компьютер"),
        "мои компьютеры": ("virtual", "MyComputerFolder", "мой компьютер"),
        "загрузки": ("known", "{374DE290-123F-4565-9164-39C4925E467B}", "загрузки"),
        "скачанные": ("known", "{374DE290-123F-4565-9164-39C4925E467B}", "загрузки"),
        "скачанные файлы": ("known", "{374DE290-123F-4565-9164-39C4925E467B}", "загрузки"),
        "рабочий стол": ("known", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "рабочий стол"),
        "изображения": ("known", "{33E28130-4E1E-4676-835A-98395C3BC3BB}", "изображения"),
        "картинки": ("known", "{33E28130-4E1E-4676-835A-98395C3BC3BB}", "изображения"),
        "музыка": ("known", "{4BD8D571-6D19-48D3-BE97-422220080E43}", "музыка"),
        "музыку": ("known", "{4BD8D571-6D19-48D3-BE97-422220080E43}", "музыка"),
        "видео": ("known", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "видео"),
        "видеозаписи": ("known", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "видео"),
        "корзина": ("virtual", "RecycleBinFolder", "корзина"),
        "корзину": ("virtual", "RecycleBinFolder", "корзина"),
    }

    @staticmethod
    def _known_folder_path(clsid: str):
        import ctypes
        clsid_hex = clsid.strip("{}")
        clsid_hex = clsid_hex.replace("-", "")
        d1 = int(clsid_hex[0:8], 16)
        d2 = int(clsid_hex[8:12], 16)
        d3 = int(clsid_hex[12:16], 16)
        raw = (d1.to_bytes(4, "little") + d2.to_bytes(2, "little") +
               d3.to_bytes(2, "little") + bytes.fromhex(clsid_hex[16:]))
        shell = ctypes.windll.shell32
        ole = ctypes.windll.ole32
        g = ctypes.create_string_buffer(16)
        g.raw = raw
        p = ctypes.c_char_p()
        ret = shell.SHGetKnownFolderPath(
            ctypes.cast(g, ctypes.POINTER(ctypes.c_byte)), 0, None, ctypes.byref(p))
        if ret != 0:
            return ""
        path = ctypes.wstring_at(p)
        ole.CoTaskMemFree(p)
        return path

    def _open_folder(self, text: str) -> bool:
        t = text.lower()
        target = None
        folder_name = None
        kind = None
        for alias, (k, ident, name) in self.SHELL_FOLDERS.items():
            if alias in t:
                kind = k
                target = ident
                folder_name = name
                break
        if kind is None:
            # «открой папку <путь\или\имя>»
            m = re.search(r"(?:открой|откройте|открыть)\s+папку\s+(.+)", text, re.I)
            if m:
                p = m.group(1).strip(" .,;:")
                p = p.replace("точка", ".").replace("двоеточие", ":")
                if p:
                    if p.find(":") == -1 and "\\" not in p:
                        p = os.path.join(os.path.expanduser("~"), p)
                    target = os.path.normpath(p)
                    folder_name = os.path.basename(target)
                    kind = "path"
        if kind is None or not target:
            return False
        log(f"[skill] открываю папку({kind}): {target}")
        try:
            if folder_name and folder_name != os.path.basename(str(target)):
                self._say(f"Открываю {folder_name}")
            else:
                self._say("Открываю папку")
            if kind == "known":
                real = self._known_folder_path(target)
                os.startfile(real if real else target)
            elif kind == "virtual":
                subprocess.Popen(
                    ["explorer.exe", "shell:" + target],
                    creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.startfile(target)
            return True
        except Exception as e:
            log(f"[skill] open folder error: {e}")
            self._say("Не получилось открыть папку")
            return True

    SITE_ALIASES = {
        "ютуб": "youtube.com",
        "ютьюб": "youtube.com",
        "youtube": "youtube.com",
        "ютюб": "youtube.com",
"вконтакте": "vk.com",
        "вк": "vk.com",
        "вы ка": "vk.com",
        "выка": "vk.com",
        "в ка": "vk.com",
        "века": "vk.com",
        "вка": "vk.com",
        "вк точка": "vk.com",
        "яндекс": "ya.ru",
        "yandex": "ya.ru",
        "гугл": "google.com",
        "google": "google.com",
        "гмейл": "mail.google.com",
        "gmail": "mail.google.com",
        "почта": "mail.ru",
        "мэйл": "mail.ru",
        "mail": "mail.ru",
        "github": "github.com",
        "гитхаб": "github.com",
        "телеграм": "web.telegram.org",
        "telegram": "web.telegram.org",
        "википедия": "ru.wikipedia.org",
        "wikipedia": "wikipedia.org",
    }

    def _extract_site(self, text: str) -> str:
        t = text.lower()
        for alias, url in self.SITE_ALIASES.items():
            if alias in t:
                return url
        match = re.search(r"(?:сайт|открой|открывай)\s+(\S+)", t)
        if match:
            return match.group(1).replace("точка", ".").replace("ком", ".com") \
                .replace("ру", ".ru") \
                .replace(" ", "")
        return ""

    def _open_site(self, site: str):
        if not site.startswith("http"):
            site = "https://" + site
        log(f"[skill] открываю сайт: {site}")
        try:
            os.startfile(site)
            self._say("Открываю сайт")
        except Exception as e:
            log(f"[skill] open site error: {e}")
            self._say("Не получилось открыть сайт")

    def _shutdown(self):
        self._say("Выключаю компьютер")
        log("[skill] shutdown")
        subprocess.Popen(["shutdown", "/s", "/t", "5", "/c", "Отключаюсь по команде Джарвиса"],
                         creationflags=subprocess.CREATE_NO_WINDOW)

    def _screen_on(self):
        self._say("Включаю экран")
        log("[skill] screen on")
        import ctypes
        ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, -1)
        ctypes.windll.user32.mouse_event(1, 0, 0, 0, 0)

    def _screen_off(self):
        self._say("Отключаю экран")
        log("[skill] screen off")
        import ctypes
        ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 1)

    def _restart(self):
        self._say("Перезагружаю компьютер")
        log("[skill] restart")
        subprocess.Popen(["shutdown", "/r", "/t", "10", "/c", "Перезагрузка по команде Джарвиса"],
                         creationflags=subprocess.CREATE_NO_WINDOW)

    def _volume(self, text: str) -> bool:
        t = text.lower()
        try:
            import ctypes
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            vol_range = volume.GetVolumeRange()
            min_v, max_v = vol_range[0], vol_range[1]
            current = volume.GetMasterVolumeLevelScalar()
            step = 0.08
            if any(k in t for k in ("громче", "громкость выше", "прибавь", "увеличь", "погромче",
                                     "сделай громче")):
                new_val = min(1.0, current + step)
                volume.SetMasterVolumeLevelScalar(new_val, None)
                pct = int(new_val * 100)
                self._say(f"Громкость {pct} процентов")
                log(f"[skill] volume up -> {pct}%")
                return True
            if any(k in t for k in ("тише", "громкость ниже", "убавь", "уменьши", "потише",
                                     "сделай тише")):
                new_val = max(0.0, current - step)
                volume.SetMasterVolumeLevelScalar(new_val, None)
                pct = int(new_val * 100)
                self._say(f"Громкость {pct} процентов")
                log(f"[skill] volume down -> {pct}%")
                return True
            if "процент" in t or "%" in t:
                match = re.search(r"(\d{1,3})", t)
                if match:
                    pct = max(0, min(100, int(match.group(1))))
                    volume.SetMasterVolumeLevelScalar(pct / 100.0, None)
                    self._say(f"Громкость {pct} процентов")
                    log(f"[skill] volume set -> {pct}%")
                    return True
            return False
        except Exception as e:
            log(f"[skill] volume error: {e}")
            self._say("Не могу управлять громкостью")
            return True

    def _find_file(self, text: str) -> bool:
        import threading
        name = self._extract_file_query(text)
        if not name:
            self._say("Что искать? Скажите, например, найди файл отчёт")
            return True
        self._say(f"Ищу файл {name}. Это может занять пару секунд")
        threading.Thread(target=self._search_file_thread, args=(name,), daemon=True).start()
        return True

    def _extract_file_query(self, text: str) -> str:
        t = text
        for k in ("найди файл", "найти файл", "поиск файла", "найди документ",
                  "поиск на компьютере", "найди на компьютере", "найди"):
            if k in t:
                t = t.split(k, 1)[1]
                break
        t = t.replace("где лежит", "").strip(" .,;:-")
        t = re.sub(r"\s+", " ", t).strip(" .,;:-")
        t = re.sub(r"(пожалуйста|на компьютере)$", "", t).strip()
        return t

    def _search_file_thread(self, name: str):
        import os as _os
        from pathlib import Path
        user = Path(_os.path.expanduser("~"))
        roots = []
        known = {
            "Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos",
        }
        for rid in (user / "OneDrive" / "Desktop", user / "Desktop",
                    user / "OneDrive" / "Документы", user / "Documents",
                    user / "Downloads", user / "OneDrive" / "Documents"):
            if rid.is_dir():
                roots.append(str(rid))
        # добавить стандартные папки по их известным именам
        if not roots:
            for d in user.iterdir() if user.is_dir() else []:
                try:
                    if d.is_dir() and d.name.lower() in {x.lower() for x in known}:
                        roots.append(str(d))
                except Exception:
                    pass
        if not roots:
            roots = [str(user)]

        name_l = name.lower()
        want_ext = "." in name
        start = time.time()
        results = []
        scanned = 0
        for root in roots:
            if time.time() - start > 25:
                break
            for dirpath, dirnames, filenames in _os.walk(root):
                if time.time() - start > 25:
                    break
                for f in filenames:
                    scanned += 1
                    if scanned > 60000:
                        break
                    fl = f.lower()
                    hit = name_l in fl
                    if want_ext:
                        hit = hit and fl.endswith(name_l)
                    if hit:
                        full = _os.path.join(dirpath, f)
                        if full not in results:
                            results.append(full)
                            if len(results) >= 6:
                                break
                if len(results) >= 6:
                    break
            if len(results) >= 6:
                break
        results = results[:6]
        if results:
            rpt = "Нашёл. Например: " + "; ".join(results[:3])
            log(f"[find] {name} -> {len(results)} найдено")
            self._say(rpt)
        else:
            self._say(f"Не нашёл файл {name} в стандартных папках")

    APP_ALIASES = {
        "блокнот": "notepad.exe",
        "ноутпад": "notepad.exe",
        "калькулятор": "calc.exe",
        "проводник": "explorer.exe",
        "краски": "mspaint.exe",
        "пейнт": "mspaint.exe",
        "паинт": "mspaint.exe",
        "командная строка": "cmd.exe",
        "терминал": "cmd.exe",
        "диспетчер задач": "taskmgr.exe",
        "диспетчер": "taskmgr.exe",
        "таскменеджер": "taskmgr.exe",
        "ворд": "winword.exe",
        "вордпад": "wordpad.exe",
        "эксель": "excel.exe",
        "повер поинт": "powerpnt.exe",
        "паверпоинт": "powerpnt.exe",
        "презентации": "powerpnt.exe",
        "аутлук": "outlook.exe",
        "почта": "outlook.exe",
        "скайп": "skype.exe",
        "телеграм": "telegram.exe",
        "телеграмм": "telegram.exe",
        "стим": "steam.exe",
        "змв": "zoom.exe",
    }

    def _find_app(self, t: str) -> str:
        for name, exe in self.APP_ALIASES.items():
            if name in t:
                return exe
        words = re.sub(r"(открой|запусти|включи|откройте|запустите|пожалуйста|программу|приложение)", " ", t)
        words = re.sub(r"\s+", " ", words).strip()
        if len(words) > 1:
            return words
        return ""

    def _launch_app(self, app: str):
        if os.sep not in app and not app.lower().endswith(".exe"):
            app = app + ".exe"
        log(f"[skill] запуск: {app}")
        try:
            subprocess.Popen([app], shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self._say("Запускаю")
        except Exception as e:
            log(f"[skill] launch error: {e}")
            self._say("Не получилось запустить")

    def _extract_note(self, text: str) -> str:
        for k in ("запиши", "запомни", "заметка", "запишите"):
            if k in text:
                note = text.split(k, 1)[1].strip(" ,.:;-")
                if note:
                    return note
        return ""

    def _save_note(self, note: str):
        from datetime import datetime
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "заметки.txt")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | {note}\n")
            self._say("Записал в заметки")
            log(f"[skill] заметка сохранена: {note}")
        except Exception as e:
            log(f"[skill] note error: {e}")
            self._say("Не получилось записать заметку")

    def _tell_joke(self):
        jokes = [
            "Почему программисты не любят природу? Слишком много багов.",
            "Как называется программист без девушки? Холостой.",
            "Сколько программистов нужно, чтобы вкрутить лампочку? Ни одного, это аппаратная проблема.",
            "Почему утюг не работает? Потому что он не включен в сеть.",
            "Что говорит один кирпич другому? Пошли, стенку построим."
        ]
        import random
        joke = random.choice(jokes)
        self._say(joke)

    def _tell_fairy_tale(self):
        tales = [
            "Жили-были дед и баба. Снесла курочка Ряба яичко, да не простое, а золотое. "
            "Дед бил, бил — не разбил. Баба била, била — не разбила. А мышка бежала, "
            "хвостиком махнула, яичко упало и разбилось. Дед плачет, баба плачет, а курочка кудахчет: "
            "«Не плачь, дед, не плачь, баба! Снесу я вам яичко другое, не золотое, а простое».",
            "Посадил дед репку. Выросла репка большая-пребольшая. Стал дед репку из земли тянуть: "
            "тянет-потянет, вытянуть не может. Позвал дед бабку. Бабка за дедку, дедка за репку — "
            "тянут-потянут, вытянуть не могут. Позвала бабка внучку. Внучка за бабку, бабка за дедку, "
            "дедка за репку — тянут-потянут, вытянуть не могут. Позвала внучка Жучку. Жучка за внучку, "
            "внучка за бабку, бабка за дедку, дедка за репку — тянут-потянут, вытянуть не могут. "
            "Позвала Жучка кошку. Кошка за Жучку, Жучка за внучку, внучка за бабку, бабка за дедку, "
            "дедка за репку — тянут-потянут, вытянуть не могут. Позвала кошка мышку. Мышка за кошку, "
            "кошка за Жучку, Жучка за внучку, внучка за бабку, бабка за дедку, дедка за репку — "
            "тянут-потянут, и вытянули репку!",
            "Жили-были три медведя. И был у каждого свой дом. Пошла как-то Машенька в лес, "
            "заблудилась и набрела на избушку медведей. Вошла, а там три чашки: большая, средняя и маленькая. "
            "Отведала Машенька из большой — не понравилось, из средней — тоже, а из маленькой — всё выпила. "
            "Села на большие стулья, сломала их, а на маленький стульчик села и уснула. "
            "Вернулись медведи, увидели стулья сломанные, чашки пустые, а в светёлке — Машенька спит. "
            "Проснулась Машенька, увидела медведей, выскочила в окно и убежала домой. И больше в лес одна не ходила.",
            "Жили-были дед и баба. Испекла баба колобок и положила на окошко остывать. "
            "Колобок полежал да и покатился. Катится колобок по дорожке, навстречу заяц: "
            "«Колобок, колобок, я тебя съем!» — «Не ешь меня, зайка, я тебе песенку спою». "
            "И покатился дальше. Встретил волка, потом медведя — и всем песенку спел, и все его отпустили. "
            "Катится дальше, а навстречу лиса. «Здравствуй, колобок! Спой мне песенку». "
            "Колобок спел, а лиса и говорит: «Сядь ко мне на нос да спой ещё разок». "
            "Сел колобок лисице на нос и запел. А лиса — ам! — и съела его.",
            "Стоит в поле теремок. Бежит мимо мышка-норушка, увидела теремок, стала там жить. "
            "Прискакала лягушка-квакушка, потом зайчик-побегайчик, затем лисичка-сестричка "
            "и волчок — серый бочок. Всем хватило места. Идут мимо медведь косолапый и просится жить. "
            "Не уместился медведь, полез на крышу — и сломал теремок. Звери разбежались. "
            "А потом пожалел медведь и помог построить новый теремок, ещё лучше прежнего. "
            "Стали все вместе жить-поживать и добра наживать.",
        ]
        import random
        tale = random.choice(tales)
        self._say(tale)

    def _weather(self, text: str) -> bool:
        import re
        city_match = re.search(r'в\s+([а-яё-]+)', text, re.IGNORECASE)
        city = city_match.group(1) if city_match else 'Москва'
        city_l = city.lower().capitalize()

        cond_ru = {
            "clear": "ясно", "sunny": "ясно", "overcast": "облачно",
            "cloudy": "облачно", "partly cloudy": "переменная облачность",
            "patchy rain": "небольшой дождь", "rain": "дождь",
            "light rain": "лёгкий дождь", "moderate rain": "умеренный дождь",
            "heavy rain": "сильный дождь", "drizzle": "морось",
            "thundery outbreaks": "гроза", "thunder": "гроза",
            "snow": "снег", "light snow": "лёгкий снег",
            "heavy snow": "сильный снег", "sleet": "мокрый снег",
            "fog": "туман", "mist": "туман", "haze": "дымка",
            "misty": "туман", "windy": "ветрено", "ice pellets": "ледяная крупа",
            "freezing fog": "морозный туман", "blizzard": "метель",
        }

        try:
            import requests
            headers = {'User-Agent': 'curl/8.0'}
            url = f'https://wttr.in/{city_l}?format=%t|%C'
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            body = r.text.strip().lower()
            parts = [p.strip() for p in body.split('|')]
            if not parts or not parts[0]:
                self._say(f'Не удалось получить погоду для {city_l}')
                return False
            temp = parts[0]
            cond_raw = parts[1] if len(parts) > 1 else ''
            temp_digit = re.sub(r'[^\d+-]', '', temp)
            for en, ru in cond_ru.items():
                if en in cond_raw:
                    cond = ru
                    break
            else:
                cond = cond_raw or ''
            msg = f'В {city_l} сейчас {temp_digit or temp} градусов'
            if cond:
                msg += f', {cond}'
            self._say(msg)
            return True

        except Exception as e:
            log(f'[weather] error: {e}')
            self._say(f'Ошибка при получении погоды: {e}')
            return False

    def _safe_calc(self, expr: str) -> bool:
        import ast
        try:
            words = {
                "ноль": "0", "один": "1", "два": "2", "три": "3", "четыре": "4",
                "пять": "5", "шесть": "6", "семь": "7", "восемь": "8",
                "девять": "9", "десять": "10",
                "плюс": "+", "минус": "-", "умножить на": "*", "умножить": "*",
                "разделить на": "/", "разделить": "/", "прибавить": "+",
                "отнять": "-", "возвести в степень": "**", "в степени": "**",
            }
            for w, s in words.items():
                expr = expr.replace(w, s)
            expr = expr.replace("х", "*").replace("×", "*").replace("÷", "/").replace(":", "/")
            tree = ast.parse(expr, mode="eval")
            allowed = (ast.Expression, ast.BinOp, ast.UnaryOp,
                       ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div,
                       ast.Pow, ast.Mod, ast.USub, ast.UAdd)
            for node in ast.walk(tree):
                if not isinstance(node, allowed):
                    return False
            result = eval(compile(tree, "<calc>", "eval"))
            if isinstance(result, (int, float)):
                if float(result).is_integer():
                    result = int(result)
                self._say(f"Получается {result}")
                log(f"[skill] calc: {expr} = {result}")
                return True
        except Exception as e:
            log(f"[skill] calc error: {e}")
        return False

    def _play_mp3_blocking(self, tmp: str):
        try:
            import ctypes
            ctypes.windll.winmm.mciSendStringW(
                f'open "{tmp}" type mpegvideo alias jv', None, 0, 0)
            ctypes.windll.winmm.mciSendStringW('play jv wait', None, 0, 0)
            ctypes.windll.winmm.mciSendStringW('close jv', None, 0, 0)
        except Exception as e:
            log(f"[tts] mci playback error: {e}")
            try:
                ps = ("Add-Type -AssemblyName PresentationCore; "
                      f"$p = New-Object System.Windows.Media.MediaPlayer; "
                      f"$p.Open([uri]'{tmp}'); $p.Play(); "
                      "Start-Sleep -Seconds 30; $p.Stop()")
                subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception as e2:
                log(f"[tts] fallback play error: {e2}")

    # ---------- TTS ----------
    async def speak(self, text: str):
        self.speaking_lock = True
        try:
            text = re.sub(r"[#*_`\[\](){}]", "", text)
            text = re.sub(r"\s+", " ", text).strip()
            # Ограничение длины: короткие ответы напр., сказки — до 4000 символов
            if not text or len(text) > 4000:
                text = text[:4000]
            if re.search(r'[\u4e00-\u9fff]', text):
                log('[filter] китаиские символы подавлены (speak)')
                return
            set_speech(text)
            try:
                import edge_tts
                tmp = os.path.join(tempfile.gettempdir(), f"jarvis_tts_{int(time.time() * 1000)}.mp3")
                last_err = None
                for attempt in range(3):
                    try:
                        comm = edge_tts.Communicate(text, VOICE)
                        await comm.save(tmp)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        await asyncio.sleep(1.0)
                if last_err:
                    log(f"[tts] gen error (3 попытки): {last_err}")
                    return
                await asyncio.to_thread(self._play_mp3_blocking, tmp)
                await asyncio.sleep(0.6)
            except Exception as e:
                log(f"[tts] error: {e}")
        finally:
            self.speaking_lock = False
            set_state("idle")

    # ---------- route to MultiTool window ----------
    def route_to_multitool(self, text: str):
        try:
            wins = gw.getWindowsWithTitle(MULTITOOL_WINDOW)
            if not wins:
                log("[route] окно MultiTool не найдено")
                self._say("Не вижу окно мультитул")
                return
            win = wins[0]
            self._activate_window(win)
            time.sleep(0.8)
            pyautogui.click(win.left + win.width // 2, win.bottom - 60)
            time.sleep(0.3)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.hotkey("ctrl", "a")
            pyperclip.copy(text)
            time.sleep(0.2)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            pyautogui.press("enter")
            log("[route] команда отправлена в MultiTool")
            self._say("Принял, выполняю")
        except Exception as e:
            log(f"[route] error: {e}")
            self._say("Не смог отправить команду в мультитул")

    def _start_avatar(self):
        # Оболочка запускается В ТОМ ЖЕ процессе (фоновые потоки HTTP-сервера),
        # чтобы работать и из исходников, и из собранного exe. В frozen-режиме
        # sys.executable == Jarvis.exe, и запуск подпроцессом невозможен.
        try:
            import jarvis_avatar as av
            av.BASE = ASSET_DIR
            av.STATE_FILE = STATE_FILE
            port = av.start_server()
            html_file = "jarvis_avatar_3d.html"
            if not os.path.exists(os.path.join(av.BASE, html_file)) or \
               not os.path.exists(os.path.join(av.BASE, "three147.min.js")) or \
               not os.path.exists(os.path.join(av.BASE, "robot.glb")):
                html_file = "jarvis_avatar.html"
            url = f"http://127.0.0.1:{port}/{html_file}?t={int(time.time())}"
            edge = av.find_edge()
            if edge:
                subprocess.Popen(
                    [edge, f"--app={url}", "--window-size=300,540", "--new-window"],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            import webbrowser
            webbrowser.open(url)
            self._avatar_port = port
            self._avatar_html = html_file
            log(f"[avatar] оболочка запущена ({html_file}, порт {port})")
        except Exception as e:
            log(f"[avatar] error: {e}")

    def _activate_window(self, win):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = win._hWnd
            if win.isMinimized:
                user32.ShowWindow(hwnd, 9)
                time.sleep(0.4)
            user32.ShowWindow(hwnd, 5)
            user32.SetForegroundWindow(hwnd)
        except Exception as e:
            log(f"[activate] error: {e}")

    # ---------- OCR of the answer ----------
    def grab_answer_ocr(self) -> str:
        try:
            from winsdk.windows.media.ocr import OcrEngine
            from winsdk.windows.globalization import Language
            from winsdk.windows.graphics.imaging import BitmapDecoder
            from winsdk.windows.storage.streams import (
                InMemoryRandomAccessStream, DataWriter
            )
            import mss
            from PIL import Image
            import io as _io

            wins = gw.getWindowsWithTitle(MULTITOOL_WINDOW)
            if not wins:
                return ""
            win = wins[0]
            with mss.MSS() as sct:
                mon = {
                    "left": win.left,
                    "top": win.top,
                    "width": win.width,
                    "height": win.height,
                }
                shot = sct.grab(mon)
                img = Image.frombytes("RGB", shot.size, shot.rgb)
                buf = _io.BytesIO()
                img.save(buf, format="PNG")
                data = buf.getvalue()

            engine = OcrEngine.try_create_from_language(Language("ru-RU"))
            if engine is None:
                engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                return ""

            async def _run():
                stream = InMemoryRandomAccessStream()
                writer = DataWriter(stream.get_output_stream_at(0))
                writer.write_bytes(data)
                await writer.store_async()
                decoder = await BitmapDecoder.create_async(stream)
                sb = await decoder.get_software_bitmap_async()
                result = await engine.recognize_async(sb)
                return result.text

            return asyncio.run(_run())
        except Exception as e:
            log(f"[ocr] error: {e}")
            return ""

    async def speak_answer_from_screen(self):
        await asyncio.sleep(6.0)
        text = self.grab_answer_ocr()
        if text:
            log(f"[ocr] {text[:120]}...")
            await self.speak(text)
        else:
            log("[ocr] пусто")

    # ---------- main loop ----------
    def _greet(self):
        log("[greet] приветствие")
        self._say("Джарвис запущен. Готов к работе, мастер")

    def run(self):
        self.loop = asyncio.new_event_loop()
        t_loop = threading.Thread(target=self._loop_thread, daemon=True)
        t_loop.start()
        stop = threading.Event()
        cap = threading.Thread(target=self.run_capture, args=(stop,), daemon=True)
        cap.start()
        set_state("idle")
        self._start_avatar()
        log("Jarvis готов. Скажите «Джарвис»...")
        log("Для выхода: Ctrl+C или скажите «Джарвис, стоп».")
        threading.Timer(2.0, self._greet).start()
        threading.Thread(target=self._preload_wake, daemon=True).start()
        try:
            self.recognize_stream(stop)
        except KeyboardInterrupt:
            log("Завершаю работу...")
        finally:
            stop.set()

    def _loop_thread(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()


if __name__ == "__main__":
    j = JarvisBridge()
    j.run()
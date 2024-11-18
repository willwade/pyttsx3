import ctypes
import os
import platform
import subprocess
import time
import wave
from tempfile import NamedTemporaryFile
import logging

logger = logging.getLogger(__name__)

if platform.system() == "Windows":
    import winsound

from ..voice import Voice
from . import _espeak


# noinspection PyPep8Naming
def buildDriver(proxy):
    return EspeakDriver(proxy)


# noinspection PyPep8Naming
class EspeakDriver:
    _moduleInitialized = False
    _defaultVoice = ""

    def __init__(self, proxy):
        if not EspeakDriver._moduleInitialized:
            # espeak cannot initialize more than once per process and has
            # issues when terminating from python (assert error on close)
            # so just keep it alive and init once
            rate = _espeak.Initialize(_espeak.AUDIO_OUTPUT_RETRIEVAL, 1000)
            if rate == -1:
                raise RuntimeError("could not initialize espeak")
            current_voice = _espeak.GetCurrentVoice()
            if current_voice and current_voice.contents.name:
                EspeakDriver._defaultVoice = current_voice.contents.name.decode("utf-8")
            else:
                # Fallback to a known default if no voice is set
                EspeakDriver._defaultVoice = "gmw/en"  # Adjust this as needed
            EspeakDriver._moduleInitialized = True
        self._proxy = proxy
        self._queue = []
        self._looping = False
        self._stopping = False
        self._speaking = False
        self._text_to_say = None
        self._data_buffer = b""
        self._numerise_buffer = []
        self._save_file = None

        _espeak.SetSynthCallback(self._onSynth)
        self.setProperty("voice", EspeakDriver._defaultVoice)
        self.setProperty("rate", 200)
        self.setProperty("volume", 1.0)

    def numerise(self, data):
        self._numerise_buffer.append(data)
        return ctypes.c_void_p(len(self._numerise_buffer))

    def decode_numeric(self, data):
        return self._numerise_buffer[int(data) - 1]

    @staticmethod
    def destroy():
        _espeak.SetSynthCallback(None)

    def stop(self):
        if _espeak.IsPlaying():
            self._stopping = True
            _espeak.Cancel()

    @staticmethod
    def getProperty(name: str):
        if name == "voices":
            voices = []
            for v in _espeak.ListVoices(None):
                # Use identifier as the unique ID
                voice_id = v.identifier.decode(
                    "utf-8"
                ).lower()  # Identifier corresponds to the "File" in espeak --voices
                kwargs = {
                    "id": voice_id,  # Use "identifier" as the ID
                    "name": v.name.decode("utf-8"),  # Nice name
                }
                if v.languages:
                    try:
                        language_code_bytes = v.languages[1:]
                        language_code = language_code_bytes.decode(
                            "utf-8", errors="ignore"
                        )
                        kwargs["languages"] = [language_code]
                    except UnicodeDecodeError:
                        kwargs["languages"] = ["Unknown"]
                genders = [None, "Male", "Female"]
                kwargs["gender"] = genders[v.gender]
                kwargs["age"] = v.age or None
                voices.append(Voice(**kwargs))
            return voices
        if name == "voice":
            voice = _espeak.GetCurrentVoice()
            if voice and voice.contents.name:
                return voice.contents.identifier.decode("utf-8").lower()
            return None
        if name == "rate":
            return _espeak.GetParameter(_espeak.RATE)
        if name == "volume":
            return _espeak.GetParameter(_espeak.VOLUME) / 100.0
        if name == "pitch":
            return _espeak.GetParameter(_espeak.PITCH)
        raise KeyError("unknown property %s" % name)

    @staticmethod
    def setProperty(name: str, value):
        if name == "voice":
            if value is None:
                return
            try:
                utf8Value = str(value).encode("utf-8")
                logging.debug(f"Attempting to set voice to: {value}")
                result = _espeak.SetVoiceByName(utf8Value)
                if result == 0:  # EE_OK is 0
                    logging.debug(f"Successfully set voice to: {value}")
                elif result == 1:  # EE_BUFFER_FULL
                    raise ValueError(
                        f"SetVoiceByName failed: EE_BUFFER_FULL while setting voice to {value}"
                    )
                elif result == 2:  # EE_INTERNAL_ERROR
                    raise ValueError(
                        f"SetVoiceByName failed: EE_INTERNAL_ERROR while setting voice to {value}"
                    )
                else:
                    raise ValueError(
                        f"SetVoiceByName failed with unknown return code {result} for voice: {value}"
                    )
            except ctypes.ArgumentError as e:
                raise ValueError(f"Invalid voice name: {value}, error: {e}")
        elif name == "rate":
            try:
                _espeak.SetParameter(_espeak.RATE, value, 0)
            except ctypes.ArgumentError as e:
                raise ValueError(str(e))
        elif name == "volume":
            try:
                _espeak.SetParameter(_espeak.VOLUME, int(round(value * 100, 2)), 0)
            except TypeError as e:
                raise ValueError(str(e))
        elif name == "pitch":
            try:
                _espeak.SetParameter(_espeak.PITCH, int(value), 0)
            except TypeError as e:
                raise ValueError(str(e))
        else:
            raise KeyError("unknown property %s" % name)


    def _start_synthesis(self, text):
        self._proxy.setBusy(True)
        self._proxy.notify("started-utterance")
        self._speaking = True
        self._data_buffer = b""  # Ensure buffer is cleared before starting
        try:
            _espeak.Synth(
                str(text).encode("utf-8"), flags=_espeak.ENDPAUSE | _espeak.CHARS_UTF8
            )
        except Exception as e:
            self._proxy.setBusy(False)
            self._proxy.notify("error", exception=e)
            raise

    def _onSynth(self, wav, numsamples, events):
        logger.debug(f"[DEBUG] Synth callback invoked with {numsamples} samples")
        logger.debug(f"[DEBUG] Speaking: {self._speaking}")
        logger.debug(f"[DEBUG] Queue: {self._queue}")

        for event in events:
            logger.debug(f"[DEBUG] Event: {event.type}")

            if event.type == _espeak.EVENT_LIST_TERMINATED:
                logger.debug("[DEBUG] Event: LIST_TERMINATED - Finalizing.")

                # Check if we were saving to a file
                if self._save_file:
                    try:
                        with wave.open(self._save_file, "wb") as f:
                            f.setnchannels(1)
                            f.setsampwidth(2)
                            f.setframerate(22050)
                            f.writeframes(self._data_buffer)
                        logger.debug(f"[DEBUG] Audio saved to {self._save_file}")
                    except Exception as e:
                        logger.error(f"[ERROR] Failed to save audio to file: {e}")
                    finally:
                        self._data_buffer = b""  # Clear buffer
                        self._save_file = None  # Reset save_file flag
                
                # Reset speaking and notify completion
                self._speaking = False
                self._proxy.notify("finished-utterance", completed=True)
                self._proxy.setBusy(False)  # Reset busy state

        # Process synthesized audio
        if numsamples > 0:
            self._data_buffer += ctypes.string_at(wav, numsamples * ctypes.sizeof(ctypes.c_short))

        return 0

    def _playback_audio(self):
        try:
            with NamedTemporaryFile(suffix=".wav", delete=False) as temp_wav:
                with wave.open(temp_wav, "wb") as f:
                    f.setnchannels(1)
                    f.setsampwidth(2)
                    f.setframerate(22050)
                    f.writeframes(self._data_buffer)

                temp_wav_name = temp_wav.name
                temp_wav.flush()

            logger.debug(f"[DEBUG] Playback temp file: {temp_wav_name}")

            if platform.system() == "Darwin":
                subprocess.run(["afplay", temp_wav_name], check=True)
            elif platform.system() == "Linux":
                os.system(f"aplay {temp_wav_name} -q")
            elif platform.system() == "Windows":
                winsound.PlaySound(temp_wav_name, winsound.SND_FILENAME)

            os.remove(temp_wav_name)
        except Exception as e:
            logger.error(f"[ERROR] Playback error: {e}")

    def endLoop(self):
        self._looping = False


    def startLoop(self, external=False):
        if self._looping:
            logger.debug("[DEBUG] Loop already active; skipping startLoop.")
            return

        logger.debug("[DEBUG] Starting loop")
        self._looping = True
        self._is_external_loop = external

        timeout_counter = 0  # Timeout counter for debugging purposes

        while self._looping:
            logger.debug(f"[DEBUG] Loop state - Queue: {self._queue}, Speaking: {self._speaking}")

            # Timeout logic to avoid infinite loops during debugging
            timeout_counter += 1
            if timeout_counter > 5000:  # Example: 5000 iterations as a safeguard
                logger.error("[ERROR] Loop timeout - Exiting for debugging.")
                self._looping = False
                break

            # If not currently speaking, fetch the next task in the queue
            if not self._speaking and self._queue:
                task = self._queue.pop(0)
                if isinstance(task, dict) and "filename" in task:
                    logger.debug(f"[DEBUG] Processing save-to-file task: {task}")
                    self._save_file = task["filename"]
                    self._text_to_say = task["text"]
                    self._proxy.setBusy(True)  # Mark as busy when processing a task
                    self._start_synthesis(self._text_to_say)
                else:
                    logger.debug(f"[DEBUG] Processing say task: {task}")
                    self._save_file = None
                    self._text_to_say = task
                    self._proxy.setBusy(True)  # Mark as busy when processing a task
                    self._start_synthesis(self._text_to_say)

            # Exit the loop when there are no tasks and speaking has stopped
            if not self._speaking and not self._queue:
                logger.debug("[DEBUG] Queue is empty and not speaking; exiting loop.")
                self._looping = False

            try:
                if external:
                    next(self.iterate())
                else:
                    time.sleep(0.01)
            except StopIteration:
                logger.debug("[DEBUG] Stopping loop due to StopIteration")
                break

        self._proxy.setBusy(False)  # Ensure the proxy is not busy after loop ends
        logger.debug("[DEBUG] Exiting loop.")
        
    def say(self, text):
        logger.debug(f"[DEBUG] EspeakDriver.say called with text: {text}")
        self._queue.append(text)  # Add text to the queue
        logger.debug(f"[DEBUG] Updated Queue: {self._queue}")
        if not self._looping:
            self.startLoop()

    def save_to_file(self, text, filename):
        logger.debug(f"[DEBUG] EspeakDriver.save_to_file called with text: {text} and filename: {filename}")
        self._queue.append({"text": text, "filename": filename})  # Add save-to-file task
        logger.debug(f"[DEBUG] Updated Queue: {self._queue}")
        if not self._looping:
            self.startLoop()
            
    def runAndWait(self):
        """
        Run the event loop until all tasks in the queue are processed.
        """
        logger.debug("[DEBUG] EspeakDriver.runAndWait called")
        if not self._looping:
            self.startLoop()

        # Wait for the queue and speaking tasks to complete
        while self._queue or self._speaking:
            time.sleep(0.01)

        logger.debug("[DEBUG] runAndWait completed - Queue empty, not speaking.")
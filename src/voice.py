"""Entrada de voz 100% local con faster-whisper (multilingue, incluye español).

Corre en CPU sin GPU ni API keys. Las dependencias (faster-whisper,
sounddevice) son opcionales y se importan perezosamente para que la CLI
siga funcionando en modo texto si no estan instaladas.
"""


class WhisperListener:
    """Graba del microfono (push-to-talk) y transcribe en español."""

    def __init__(self, model_size: str = "small", language: str = "es",
                 samplerate: int = 16000):
        self.model_size = model_size
        self.language = language
        self.samplerate = samplerate
        self._model = None

    @staticmethod
    def available() -> bool:
        """True si las dependencias de voz estan instaladas."""
        try:
            import faster_whisper  # noqa: F401
            import sounddevice  # noqa: F401
            return True
        except ImportError:
            return False

    def load(self):
        """Carga el modelo Whisper (se descarga de HuggingFace la 1a vez)."""
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.model_size, device="cpu", compute_type="int8"
            )
        return self._model

    def record(self):
        """Graba audio del microfono hasta que el usuario presione Enter."""
        import numpy as np
        import sounddevice as sd

        frames = []

        def _callback(indata, frame_count, time_info, status):
            frames.append(indata.copy())

        with sd.InputStream(samplerate=self.samplerate, channels=1,
                            dtype="float32", callback=_callback):
            input()  # Enter detiene la grabacion

        if not frames:
            return np.zeros(0, dtype="float32")
        return np.concatenate(frames, axis=0).ravel()

    def transcribe(self, audio) -> str:
        """Transcribe el audio en español. '' si es demasiado corto."""
        if audio.size < self.samplerate // 2:  # menos de 0.5s de audio
            return ""
        model = self.load()
        segments, _ = model.transcribe(
            audio,
            language=self.language,
            vad_filter=True,  # recorta silencios
            beam_size=1,      # velocidad sobre precision (es un demo)
        )
        return " ".join(seg.text for seg in segments).strip()

    def listen(self) -> str:
        """Atajo: graba y transcribe."""
        return self.transcribe(self.record())

"""
Speech-to-Text module using Whisper.
"""

import asyncio
import os
from typing import Optional

import numpy as np
from loguru import logger


class WhisperSTT:
    """Whisper-based Speech-to-Text."""
    
    def __init__(
        self,
        model_name: str = "base",
        device: str = "auto",
        language: str = "en",
    ):
        self.model_name = model_name
        self.device = device
        self.language = language
        self.model = None
        self._backend = "mock"
        self._load_model()
    
    def _load_model(self):
        """Load the Whisper model."""
        # Try faster-whisper first
        try:
            from faster_whisper import WhisperModel
            
            if self.device == "auto":
                import torch
                if torch.cuda.is_available():
                    self.device = "cuda"
                    compute_type = "float16"
                elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                    self.device = "cpu"
                    compute_type = "int8"
                else:
                    self.device = "cpu"
                    compute_type = "int8"
            elif self.device == "cuda":
                compute_type = "float16"
            else:
                compute_type = "int8"
            
            logger.info(f"Loading faster-whisper {self.model_name} on {self.device}")
            self.model = WhisperModel(
                self.model_name,
                device=self.device if self.device != "mps" else "cpu",
                compute_type=compute_type,
            )
            self._backend = "faster-whisper"
            logger.info("✅ faster-whisper loaded")
            return
        except ImportError:
            logger.warning("faster-whisper not available")
        except Exception as e:
            logger.warning(f"faster-whisper failed: {e}")
        
        # Try openai-whisper
        try:
            import whisper
            
            if self.device == "auto":
                import torch
                self.device = "cuda" if torch.cuda.is_available() else "cpu"
            
            logger.info(f"Loading openai-whisper {self.model_name}")
            self.model = whisper.load_model(self.model_name, device=self.device)
            self._backend = "openai-whisper"
            logger.info("✅ openai-whisper loaded")
            return
        except ImportError:
            logger.warning("openai-whisper not available")
        except Exception as e:
            logger.warning(f"openai-whisper failed: {e}")
        
        # Try 60db cloud STT (third in the chain — only kicks in if both
        # Whisper variants are missing AND the user explicitly set up 60db).
        if os.environ.get("SIXTYDB_API_KEY"):
            try:
                from . import sixtydb as sixtydb_client  # noqa: F401
                self._backend = "60db"
                logger.info("✅ 60db STT ready (cloud)")
                return
            except ImportError as e:
                logger.warning(f"60db client unavailable: {e}")
            except Exception as e:
                logger.warning(f"60db STT init failed: {e}")

        # Mock mode for testing
        logger.warning("⚠️ No STT backend - using mock mode")
        self._backend = "mock"
    
    async def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe audio to text."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio)
    
    def _transcribe_sync(self, audio: np.ndarray) -> str:
        """Synchronous transcription."""
        if self._backend == "faster-whisper":
            segments, info = self.model.transcribe(
                audio,
                language=self.language,
                beam_size=5,
                vad_filter=True,
            )
            return " ".join(segment.text for segment in segments).strip()
        
        elif self._backend == "openai-whisper":
            result = self.model.transcribe(audio, language=self.language)
            return result["text"].strip()

        elif self._backend == "60db":
            # 60db /stt is async — we're inside an executor so spinning up
            # a one-shot loop is the simplest bridge that doesn't change
            # the existing sync `_transcribe_sync` contract.
            try:
                from . import sixtydb as sixtydb_client
                return asyncio.run(
                    sixtydb_client.transcribe_rest(
                        audio,
                        sample_rate=16000,
                        language=None if self.language == "auto" else self.language,
                    )
                )
            except Exception as e:
                logger.error(f"60db STT error: {e}")
                return ""

        else:
            # Mock mode - return placeholder
            logger.debug(f"Mock STT: received {len(audio)} samples")
            return "[Mock transcription - install whisper for real STT]"

from pathlib import Path

import sherpa_onnx
import soundfile as sf


# ============================================================
# PATH CONFIGURATION
# ============================================================

MODEL = "./model/model.onnx"
TOKENS = "./model/tokens.txt"
TEXT_FILE = "./tts_language_test.txt"


# ============================================================
# VOICE TO TEST
# ============================================================

TEST_NAME = "MARATHI_FEMALE"


# ============================================================
# HUMAN-LIKE TTS SETTINGS
# ============================================================

# Speech speed
#
# 1.00 = normal
# 0.95 = slightly slow
# 0.90 = natural/slightly slow
# 0.85 = conversational
# 0.80 = slow
# 0.70 = very slow

SPEECH_SPEED = 0.85


# Silence between sentences
#
# 0.20 = default
# 0.25 = slightly longer
# 0.30 = conversational pause
# 0.40 = long pause

SILENCE_SCALE = 0.25


# ============================================================
# VOICE CONFIGURATION
# ============================================================

VOICE_CONFIG = {

    # --------------------------------------------------------
    # ASSAMESE
    # --------------------------------------------------------

    "ASSAMESE_FEMALE": {
        "speaker_id": 0,
        "output": "assamese_female.wav",
    },

    "ASSAMESE_MALE": {
        "speaker_id": 1,
        "output": "assamese_male.wav",
    },


    # --------------------------------------------------------
    # BENGALI
    # --------------------------------------------------------

    "BENGALI_FEMALE": {
        "speaker_id": 2,
        "output": "bengali_female.wav",
    },

    "BENGALI_MALE": {
        "speaker_id": 3,
        "output": "bengali_male.wav",
    },


    # --------------------------------------------------------
    # BODO
    # --------------------------------------------------------

    "BODO_FEMALE": {
        "speaker_id": 4,
        "output": "bodo_female.wav",
    },

    "BODO_MALE": {
        "speaker_id": 5,
        "output": "bodo_male.wav",
    },


    # --------------------------------------------------------
    # DOGRI
    # --------------------------------------------------------

    "DOGRI_FEMALE": {
        "speaker_id": 6,
        "output": "dogri_female.wav",
    },

    "DOGRI_MALE": {
        "speaker_id": 7,
        "output": "dogri_male.wav",
    },


    # --------------------------------------------------------
    # KANNADA
    # --------------------------------------------------------

    "KANNADA_FEMALE": {
        "speaker_id": 8,
        "output": "kannada_female.wav",
    },

    "KANNADA_MALE": {
        "speaker_id": 9,
        "output": "kannada_male.wav",
    },


    # --------------------------------------------------------
    # MAITHILI
    # --------------------------------------------------------

    "MAITHILI_MALE": {
        "speaker_id": 10,
        "output": "maithili_male.wav",
    },


    # --------------------------------------------------------
    # MALAYALAM
    # --------------------------------------------------------

    "MALAYALAM_FEMALE": {
        "speaker_id": 11,
        "output": "malayalam_female.wav",
    },


    # --------------------------------------------------------
    # MARATHI
    # --------------------------------------------------------

    "MARATHI_FEMALE": {
        "speaker_id": 12,
        "output": "marathi_female.wav",
    },

    "MARATHI_MALE": {
        "speaker_id": 13,
        "output": "marathi_male.wav",
    },


    # --------------------------------------------------------
    # PUNJABI
    # --------------------------------------------------------

    "PUNJABI_FEMALE": {
        "speaker_id": 15,
        "output": "punjabi_female.wav",
    },

    "PUNJABI_MALE": {
        "speaker_id": 16,
        "output": "punjabi_male.wav",
    },


    # --------------------------------------------------------
    # SANSKRIT
    # --------------------------------------------------------

    "SANSKRIT_MALE": {
        "speaker_id": 17,
        "output": "sanskrit_male.wav",
    },


    # --------------------------------------------------------
    # TAMIL
    # --------------------------------------------------------

    "TAMIL_FEMALE": {
        "speaker_id": 18,
        "output": "tamil_female.wav",
    },


    # --------------------------------------------------------
    # TELUGU
    # --------------------------------------------------------

    "TELUGU_FEMALE": {
        "speaker_id": 19,
        "output": "telugu_female.wav",
    },
}


# ============================================================
# READ TEXT FROM FILE
# ============================================================

def get_text_from_file(test_name: str) -> str:

    file_path = Path(TEXT_FILE)

    if not file_path.exists():
        raise FileNotFoundError(
            f"Text file not found: {TEXT_FILE}"
        )

    lines = file_path.read_text(
        encoding="utf-8"
    ).splitlines()

    start_marker = (
        test_name.replace("_", " ")
        + " (Speaker ID"
    )

    collecting = False
    text_lines = []

    for line in lines:

        line = line.strip()

        # ----------------------------------------------------
        # Start selected section
        # ----------------------------------------------------

        if line.startswith(start_marker):

            collecting = True
            continue

        # ----------------------------------------------------
        # Collect selected section
        # ----------------------------------------------------

        if collecting:

            # Empty line means section ended
            if not line:
                break

            # Another speaker section started
            if "(Speaker ID" in line:
                break

            text_lines.append(line)

    text = " ".join(text_lines).strip()

    if not text:

        raise RuntimeError(
            f"Could not find text section for: {test_name}"
        )

    return text


# ============================================================
# BASIC TEXT CLEANING
# ============================================================

def prepare_text(text: str) -> str:

    # Remove unnecessary spaces
    text = " ".join(text.split())

    # Make repeated punctuation cleaner
    text = text.replace("!!!", "!")
    text = text.replace("???", "?")
    text = text.replace(",,", ",")

    return text.strip()


# ============================================================
# VALIDATE TEST VOICE
# ============================================================

if TEST_NAME not in VOICE_CONFIG:

    available = "\n".join(
        f"  - {name}"
        for name in VOICE_CONFIG.keys()
    )

    raise ValueError(
        f"\nInvalid TEST_NAME: {TEST_NAME}\n\n"
        f"Available voices:\n{available}"
    )


# ============================================================
# GET VOICE SETTINGS
# ============================================================

speaker_id = VOICE_CONFIG[TEST_NAME]["speaker_id"]

output_file = VOICE_CONFIG[TEST_NAME]["output"]


# ============================================================
# LOAD TEXT
# ============================================================

text = get_text_from_file(TEST_NAME)

text = prepare_text(text)


# ============================================================
# DISPLAY TEST INFORMATION
# ============================================================

print()
print("=" * 60)
print("             SHERPA-ONNX TTS TEST")
print("=" * 60)

print(f"Voice         : {TEST_NAME}")
print(f"Speaker ID    : {speaker_id}")
print(f"Speed         : {SPEECH_SPEED}")
print(f"Silence Scale : {SILENCE_SCALE}")

print("-" * 60)

print("Text:")
print(text)

print("=" * 60)
print()


# ============================================================
# SHERPA-ONNX CONFIGURATION
# ============================================================

config = sherpa_onnx.OfflineTtsConfig(

    model=sherpa_onnx.OfflineTtsModelConfig(

        vits=sherpa_onnx.OfflineTtsVitsModelConfig(

            model=MODEL,

            tokens=TOKENS,
        ),

        provider="cpu",

        num_threads=4,
    ),
)


# ============================================================
# VALIDATE CONFIGURATION
# ============================================================

if not config.validate():

    raise RuntimeError(
        "Invalid Sherpa-ONNX configuration."
    )


# ============================================================
# CREATE TTS ENGINE
# ============================================================

print("Loading TTS model...")

tts = sherpa_onnx.OfflineTts(config)

print("TTS model loaded successfully.")
print()


# ============================================================
# GENERATION CONFIGURATION
# ============================================================

generation_config = sherpa_onnx.GenerationConfig()


# ============================================================
# SPEAKER
# ============================================================

generation_config.sid = speaker_id


# ============================================================
# SPEECH SPEED
# ============================================================

generation_config.speed = SPEECH_SPEED


# ============================================================
# SILENCE / PAUSE
# ============================================================

generation_config.silence_scale = SILENCE_SCALE


# ============================================================
# EMOTION / STYLE
# ============================================================
#
# Your model may support model-specific "extra" parameters.
#
# emotion_id:
#
# 0  = ALEXA
# 1  = ANGER
# 2  = BB
# 3  = BOOK
# 4  = CONV
# 5  = DIGI
# 6  = DISGUST
# 7  = FEAR
# 8  = HAPPY
# 9  = INDIC
# 10 = NEWS
# 11 = NAMES
# 12 = SAD
# 13 = SANGRAH
# 14 = SURPRISE
# 15 = UMANG
# 16 = WIKI
#
# 0 is used here.
#
# IMPORTANT:
# This only has an effect if your particular model
# supports the emotion_id parameter.
# ============================================================

generation_config.extra = {
    "emotion_id": "0"
}


# ============================================================
# GENERATE SPEECH
# ============================================================

print("Generating speech...")
print("Please wait...")


audio = tts.generate(
    text,
    generation_config,
)


# ============================================================
# CHECK AUDIO
# ============================================================

if audio is None:

    raise RuntimeError(
        "TTS returned no audio."
    )


if len(audio.samples) == 0:

    raise RuntimeError(
        "No audio samples were generated."
    )


# ============================================================
# CALCULATE AUDIO DURATION
# ============================================================

duration = (
    len(audio.samples)
    / audio.sample_rate
)


# ============================================================
# SAVE WAV FILE
# ============================================================

sf.write(
    output_file,
    audio.samples,
    audio.sample_rate,
    subtype="PCM_16",
)


# ============================================================
# FINAL RESULT
# ============================================================

print()
print("=" * 60)
print("                    DONE")
print("=" * 60)

print(f"Voice         : {TEST_NAME}")
print(f"Speaker ID    : {speaker_id}")
print(f"Speed         : {SPEECH_SPEED}")
print(f"Silence Scale : {SILENCE_SCALE}")
print(f"Sample Rate   : {audio.sample_rate}")
print(f"Duration      : {duration:.2f} seconds")
print(f"Output File   : {output_file}")

print("=" * 60)
print()
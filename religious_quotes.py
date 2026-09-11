import json
import os
import re
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests


# ============================================================
# نور الدقيقة — البرنامج المجمع
#
# Pipeline:
# 1) تشغيل كود جروك.py لتوليد النص النهائي
# 2) تحويل الكلام العادي إلى صوت ElevenLabs
# 3) إذا وُجد قرآن: تنزيل التلاوة من EveryAyah ووضعها
#    في موضعها الحقيقي داخل الكلام
# 4) البحث عن خامات Portrait من Pixabay + Pexels
# 5) تنظيف الفيديوهات من صوتها، قص/تمديد الخامة حسب الزمن
# 6) تجميع كل شيء بفيديو 1080x1920 مع Effects بسيطة
# 7) إخراج MP4 جاهز على Desktop
#
# لا توجد مفاتيح API داخل هذا الملف.
# ============================================================


# ============================================================
# CONFIG
# ============================================================

# ------------------------------------------------------------
# Runtime paths: يعمل من .py أو من EXE
#
# BASE_DIR:
#   مجلد المشروع عند تشغيل .py
#   أو مجلد الـEXE عند تشغيل EXE
#
# RESOURCE_DIR:
#   نفس BASE_DIR عند تشغيل .py
#   أو مجلد الموارد المؤقت الذي يفك إليه PyInstaller عند تشغيل onefile
# ------------------------------------------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = BASE_DIR

DATA_DIR = BASE_DIR / "data"
KEYS_DIR = BASE_DIR.parent.parent / "keys"
CHANNEL_OUTPUT_DIR = BASE_DIR / "output"
TOKEN_FILE = BASE_DIR / "token.json"

# أدوات بجوار EXE/ملف Python، أو داخل موارد PyInstaller.
LOCAL_FFMPEG_DIR = BASE_DIR / "ffmpeg"
LOCAL_IMAGEMAGICK_DIR = BASE_DIR / "imagemagick"
BUNDLED_FFMPEG_DIR = RESOURCE_DIR / "ffmpeg"
BUNDLED_IMAGEMAGICK_DIR = RESOURCE_DIR / "imagemagick"

# هذه الملفات تُضمن داخل EXE عند استخدام spec.
VOICEOVER_LIBRARY = BASE_DIR / "voiceover_scripts.py"
USED_VOICEOVER_FILE = DATA_DIR / "used_voiceover_scripts.txt"
LATEST_SCRIPT = DATA_DIR / "latest_script.txt"
LATEST_REFERENCES = DATA_DIR / "latest_references.txt"
VOICEOVER_TEXT = DATA_DIR / "voiceover.txt"

WORK_DIR = DATA_DIR / "pipeline_build"
AUDIO_DIR = WORK_DIR / "audio"
QURAN_AUDIO_DIR = WORK_DIR / "quran_audio"
ASSET_DIR = WORK_DIR / "assets"
CLIP_DIR = WORK_DIR / "clips"

for folder in [
    DATA_DIR,
    WORK_DIR,
    AUDIO_DIR,
    QURAN_AUDIO_DIR,
    ASSET_DIR,
    CLIP_DIR,
]:
    folder.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------
# Secrets
# ------------------------------------------------------------

ELEVENLABS_KEY_FILE = KEYS_DIR / "elevenlabs_keys.txt"
PIXABAY_KEY_FILE = KEYS_DIR / "pixabay_api_key.txt"
PEXELS_KEY_FILE = KEYS_DIR / "pexels_api_key.txt"

# غيّره فقط لو صوت Bill عندك له ID مختلف.
ELEVENLABS_VOICE_ID = "pqHfZKP75CvOlQylNhV4"
ELEVENLABS_MODEL = "eleven_multilingual_v2"
ELEVENLABS_FORMAT = "mp3_44100_128"

# قارئ القرآن — EveryAyah
QURAN_RECITER = "Alafasy_128kbps"

# مسارات الأدوات الخارجية (Fallback لو PATH مش ظاهر للـIDE)
FFMPEG_DEFAULT_DIR = (
    LOCAL_FFMPEG_DIR
    if (
        (LOCAL_FFMPEG_DIR / "ffmpeg.exe").exists()
        and (LOCAL_FFMPEG_DIR / "ffprobe.exe").exists()
    )
    else Path(
        os.getenv(
            "FFMPEG_BIN_DIR",
            r"C:\Users\0boru\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin",
        )
    )
)

IMAGEMAGICK_DEFAULT_EXE = (
    LOCAL_IMAGEMAGICK_DIR / "magick.exe"
    if (LOCAL_IMAGEMAGICK_DIR / "magick.exe").exists()
    else Path(
        os.getenv(
            "IMAGEMAGICK_EXE",
            r"C:\Program Files\ImageMagick-7.1.2-Q16-HDRI\magick.exe",
        )
    )
)

CAPTION_FONT = os.getenv(
    "CAPTION_FONT",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
)
QURAN_JSON_PATH = RESOURCE_DIR / "religious_data" / "quran" / "quran.json"

CAPTION_TEXT_SIZE = 54
CAPTION_BOX_WIDTH = 940
CAPTION_BOX_HEIGHT = 310
CAPTION_Y = 1510


# إخراج الفيديو
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30

# عدد المشاهد التي نحاول الاحتفاظ بها
SCENE_COUNT = 8

# للبحث في Pixabay/Pexels
RESULTS_PER_QUERY = 12
MIN_ASSET_DURATION = 3

# كل مشهد يحاول استخدام خامة مختلفة.
# alternate_video_photo = None يعني اختيار الأفضل المتاح.
SCENE_PREFERENCES = [
    "video",
    "photo",
    "video",
    "photo",
    "video",
    "photo",
    "video",
    "photo",
]


# ============================================================
# HELPERS
# ============================================================

def read_secret_keys(path: Path, env_name: str) -> List[str]:
    value = os.getenv(env_name, "").strip()
    if value:
        return [line.strip() for line in value.splitlines() if line.strip()]

    if not path.exists():
        raise FileNotFoundError(
            f"ملف المفاتيح غير موجود:\n{path}\n"
            f"أو اضبط متغير البيئة {env_name}."
        )

    raw = path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    keys = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    if not keys:
        raise ValueError(f"ملف المفاتيح فارغ: {path}")

    return keys


def read_secret(path: Path, env_name: str) -> str:
    """توافق مع الأجزاء التي تحتاج مفتاحًا واحدًا (Pixabay/Pexels)."""
    keys = read_secret_keys(path, env_name)
    return keys[0]


def require_binary(name: str) -> str:
    # 1) أدوات داخل مجلد EXE/المشروع
    local = LOCAL_FFMPEG_DIR / f"{name}.exe"
    if local.exists():
        return str(local)

    # 2) أدوات مضمّنة داخل PyInstaller onefile
    bundled = BUNDLED_FFMPEG_DIR / f"{name}.exe"
    if bundled.exists():
        return str(bundled)

    # 3) PATH
    found = shutil.which(name)
    if found:
        return found

    # 4) fallback للتثبيت المحلي على جهاز المطور
    direct = FFMPEG_DEFAULT_DIR / f"{name}.exe"
    if direct.exists():
        return str(direct)

    raise RuntimeError(
        f"لم يتم العثور على {name}. "
        f"ابحث عن {name}.exe داخل ffmpeg بجوار البرنامج "
        f"أو ضمن موارد EXE."
    )


def require_magick() -> str:
    # 1) أدوات داخل مجلد EXE/المشروع
    local = LOCAL_IMAGEMAGICK_DIR / "magick.exe"
    if local.exists():
        return str(local)

    # 2) ImageMagick مضمّن داخل PyInstaller onefile
    bundled = BUNDLED_IMAGEMAGICK_DIR / "magick.exe"
    if bundled.exists():
        return str(bundled)

    # 3) PATH
    found = shutil.which("magick")
    if found:
        return found

    # 4) fallback للتثبيت المحلي على جهاز المطور
    if IMAGEMAGICK_DEFAULT_EXE.exists():
        return str(IMAGEMAGICK_DEFAULT_EXE)

    program_files = Path(
        os.getenv("ProgramFiles", r"C:\Program Files")
    )
    if program_files.exists():
        matches = list(
            program_files.glob("ImageMagick-*/magick.exe")
        )
        if matches:
            return str(sorted(matches)[-1])

    raise RuntimeError(
        "لم يتم العثور على ImageMagick (magick.exe). "
        "ضعه في imagemagick بجوار البرنامج أو ضمن موارد EXE."
    )


def run_command(
    cmd: List[str],
    label: str,
) -> None:
    print(f"\n▶️ {label}")
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        print(result.stdout[-5000:])
        raise RuntimeError(
            f"فشل الأمر: {label}\n"
            f"Exit code: {result.returncode}"
        )


def get_duration(path: Path, ffprobe: str) -> float:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"تعذر قراءة مدة الملف: {path}\n{result.stderr}"
        )

    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"مدة غير مفهومة للملف: {path}"
        ) from exc


def clean_filename(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*]', "_", text)
    text = re.sub(r"\s+", "_", text).strip("_")
    return text[:100] or "asset"


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ============================================================
# STEP 1 — GENERATE / LOAD SCRIPT
# ============================================================


def parse_ready_voiceover_library() -> List[Tuple[int, str]]:
    """
    يقرأ voiceover_scripts.py ويستخرج كل:
    نص 1:
    نص 2:
    ...
    النصوص نفسها هي المصدر النهائي، ولا يتم توليد أي Script بواسطة AI.
    """
    if not VOICEOVER_LIBRARY.exists():
        raise FileNotFoundError(
            f"ملف النصوص الجاهزة غير موجود:\n{VOICEOVER_LIBRARY}"
        )

    source_text = VOICEOVER_LIBRARY.read_text(
        encoding="utf-8",
        errors="replace",
    )

    # نقرأ محتوى VOICEOVER_SCRIPTS إن وجد، وإلا نستخدم الملف كله.
    match = re.search(
        r'VOICEOVER_SCRIPTS\s*=\s*["\']{3}([\s\S]*?)["\']{3}',
        source_text,
    )
    raw = match.group(1) if match else source_text

    pattern = re.compile(
        r'^\s*نص\s+(\d+)\s*:\s*\n'
        r'([\s\S]*?)'
        r'(?=^\s*=+\s*\n?\s*نص\s+\d+\s*:|\Z)',
        re.MULTILINE,
    )

    records = []

    for item in pattern.finditer(raw):
        number = int(item.group(1))
        text = item.group(2).strip()

        # إزالة فواصل الفصل وبعض المسافات الزائدة.
        text = re.sub(
            r'^\s*=+\s*$',
            '',
            text,
            flags=re.MULTILINE,
        ).strip()

        if text:
            records.append((number, text))

    if not records:
        raise RuntimeError(
            "لم أستطع استخراج أي نص من voiceover_scripts.py.\n"
            "تأكد من وجود صيغة: نص 1: ثم النص ثم نص 2: ..."
        )

    return records


def load_used_voiceovers() -> set:
    if not USED_VOICEOVER_FILE.exists():
        return set()

    used = set()

    for line in USED_VOICEOVER_FILE.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        line = line.strip()
        if line.isdigit():
            used.add(int(line))

    return used


def save_used_voiceover(number: int) -> None:
    USED_VOICEOVER_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(
        USED_VOICEOVER_FILE,
        "a",
        encoding="utf-8",
    ) as f:
        f.write(f"{number}\n")


def choose_random_ready_voiceover() -> Tuple[int, str, int]:
    records = parse_ready_voiceover_library()
    used = load_used_voiceovers()

    available = [
        record
        for record in records
        if record[0] not in used
    ]

    # عند استهلاك كل النصوص نبدأ دورة جديدة تلقائيًا.
    if not available:
        USED_VOICEOVER_FILE.write_text(
            "",
            encoding="utf-8",
        )
        used = set()
        available = records
        print(
            "🔄 تم استخدام كل النصوص السابقة. "
            "بدأت دورة عشوائية جديدة."
        )

    number, script = random.SystemRandom().choice(
        available
    )

    return number, script, len(records)


def build_visual_queries_from_script(
    script: str,
    count: int = SCENE_COUNT,
) -> List[str]:
    """
    لا يستخدم AI.
    يحوّل موضوع النص الجاهز إلى 8 استعلامات بصرية ثابتة/ذكية
    مناسبة لـ Pixabay وPexels.
    """
    text = normalize_spaces(script)

    categories = {
        "sleep": (
            ["نوم", "ينام", "النوم", "قبل ما تنام", "فراش"],
            [
                "peaceful bedroom at night, warm lamp, vertical",
                "person sitting on edge of bed at night, reflective mood, vertical",
                "close-up hands making dua before sleep, vertical",
                "quiet bedroom window night city lights, vertical",
                "person turning off bedside lamp, vertical",
                "open Quran on bedside table, soft light, vertical",
                "peaceful dawn through bedroom window, vertical",
                "person waking up calmly at sunrise, vertical",
            ],
        ),
        "prayer": (
            ["صلاة", "الصلاة", "الفجر", "العصر", "المغرب", "العشاء", "سجود", "وضوء"],
            [
                "Muslim man preparing for prayer, vertical",
                "wudu at mosque, close-up hands, vertical",
                "Muslim man walking toward mosque at dawn, vertical",
                "prayer mat in quiet mosque, vertical",
                "Muslim man praying in mosque, soft light, vertical",
                "close-up prayer beads and Quran, vertical",
                "mosque interior after prayer, vertical",
                "peaceful mosque exterior at sunset, vertical",
            ],
        ),
        "quran": (
            ["آية", "القرآن", "قال تعالى", "سورة", "قرآن", "كتاب الله"],
            [
                "open Quran on wooden table, soft natural light, vertical",
                "close-up Quran pages, shallow depth of field, vertical",
                "person reading Quran by window, vertical",
                "hand turning Quran page, vertical",
                "Quran beside prayer beads, vertical",
                "mosque Quran stand with soft light, vertical",
                "person reflecting while holding Quran, vertical",
                "peaceful mosque interior, vertical",
            ],
        ),
        "dua": (
            ["دعاء", "ادعي", "أدعو", "ربي", "يا رب", "استجابة"],
            [
                "Muslim man raising hands in dua, vertical",
                "close-up hands making dua, soft light, vertical",
                "person alone by window making dua, vertical",
                "night prayer and dua, cinematic vertical",
                "person sitting quietly after prayer, vertical",
                "mosque courtyard at dawn, peaceful, vertical",
                "soft sunrise over mosque, vertical",
                "calm person walking after dua, vertical",
            ],
        ),
        "charity": (
            ["صدقة", "كلمة طيبة", "ابتسامة", "مساعدة", "يتصدق", "خير"],
            [
                "person helping another person, vertical",
                "young man giving water to elderly neighbor, vertical",
                "friendly smile between two people, vertical",
                "volunteers helping in community, vertical",
                "hands giving a small gift, vertical",
                "community food donation, vertical",
                "person helping neighbor carry groceries, vertical",
                "warm human connection in city street, vertical",
            ],
        ),
        "family": (
            ["أم", "أب", "أبو", "أمك", "أبوك", "أهلك", "زوج", "زوجة", "ولد", "ابن"],
            [
                "Muslim family at home, warm natural light, vertical",
                "son talking with father at home, vertical",
                "adult child helping elderly parent, vertical",
                "mother and son talking warmly, vertical",
                "family sitting together peacefully, vertical",
                "parent and child reading Quran together, vertical",
                "family sharing a kind moment, vertical",
                "peaceful family home at sunset, vertical",
            ],
        ),
        "stress": (
            ["ضيق", "هم", "حزن", "تعب", "قلق", "خوف", "مشكلة", "ابتلاء"],
            [
                "stressed young Muslim man sitting alone, vertical",
                "person looking out window thoughtfully, vertical",
                "close-up worried face, soft cinematic light, vertical",
                "person sitting alone in quiet street, vertical",
                "hands clasped in reflection, vertical",
                "person making dua after stress, vertical",
                "sunlight breaking through clouds, vertical",
                "calm hopeful person walking at sunrise, vertical",
            ],
        ),
        "repentance": (
            ["ذنوب", "ذنب", "توبة", "تاب", "استغفار", "مغفرة", "رحمة"],
            [
                "Muslim man sitting alone in reflection, vertical",
                "person reading Quran at night, vertical",
                "hands raised in sincere repentance, vertical",
                "person praying alone in mosque, vertical",
                "close-up prayer beads in hand, vertical",
                "dark to bright light transition, vertical",
                "person walking toward mosque at dawn, vertical",
                "peaceful sunrise over mosque, vertical",
            ],
        ),
        "work": (
            ["شغل", "عمل", "رزق", "فلوس", "وظيفة", "رزق", "سعي"],
            [
                "young Muslim man working at desk, vertical",
                "home office morning light, vertical",
                "person writing goals in notebook, vertical",
                "focused worker at desk, vertical",
                "person commuting in city, vertical",
                "person taking a quiet prayer break, vertical",
                "person helping coworker, vertical",
                "calm city sunrise, vertical",
            ],
        ),
    }

    selected_category = None
    for category, (keywords, queries) in categories.items():
        if any(keyword in text for keyword in keywords):
            selected_category = queries
            break

    if selected_category is None:
        selected_category = [
            "Muslim person reflecting quietly, vertical",
            "open Quran on wooden table, soft light, vertical",
            "person walking in peaceful street, vertical",
            "hands raised in dua, vertical",
            "mosque interior with soft light, vertical",
            "person looking through window thoughtfully, vertical",
            "peaceful sunset over city, vertical",
            "sunrise near mosque, vertical",
        ]

    return selected_category[:count]


def build_ready_script_file(
    number: int,
    script: str,
    references: List[Dict[str, str]],
    scenes: List[str],
) -> None:
    """
    نحفظ نسخة منظمة للشفافية والـdebug.
    """
    lines = [
        "IDEA:",
        f"نص جاهز رقم {number}",
        "",
        "SCRIPT:",
        script.strip(),
        "",
        "RELIGIOUS_REFERENCE:",
    ]

    if references:
        for ref in references:
            lines.extend([
                f"TYPE: {ref.get('type', '')}",
                f"REFERENCE: {ref.get('source', '')}",
                "TEXT:",
                ref.get("text", ""),
                "----------------------------------------",
            ])
    else:
        lines.extend([
            "TYPE: NONE",
            "REFERENCE: NONE",
            "TEXT: NONE",
        ])

    lines.extend([
        "",
        "VISUAL_KEYWORDS:",
        ", ".join(scenes),
        "",
    ])

    for index, scene in enumerate(scenes, start=1):
        lines.extend([
            f"SCENE {index}:",
            scene,
            "",
        ])

    LATEST_SCRIPT.write_text(
        "\n".join(lines).strip() + "\n",
        encoding="utf-8",
    )

    if references:
        refs_text = "\n".join(
            [
                f"TYPE: {r.get('type','')}\n"
                f"REFERENCE: {r.get('source','')}\n"
                f"TEXT:\n{r.get('text','')}\n"
                f"{'-'*40}"
                for r in references
            ]
        )
        LATEST_REFERENCES.write_text(
            refs_text + "\n",
            encoding="utf-8",
        )
    else:
        LATEST_REFERENCES.write_text(
            "",
            encoding="utf-8",
        )



def find_quran_reference_from_local_json(script: str) -> Optional[Dict[str, str]]:
    """
    يبحث عن نص قرآني داخل السكريبت من قاعدة البيانات المحلية إن وُجدت.
    يدعم religious_data.py أو أحدث قاعدة محلية إذا كانت متاحة.
    """
    try:
        import importlib.util

        candidates = [
            RESOURCE_DIR / "religious_data.py",
            RESOURCE_DIR / "religious_data" / "religious_data.py",
        ]

        module_path = next(
            (path for path in candidates if path.exists()),
            None,
        )

        if module_path is None:
            return None

        spec = importlib.util.spec_from_file_location(
            "local_religious_data",
            module_path,
        )
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        database = getattr(
            module,
            "RELIGIOUS_DATABASE",
            [],
        )

        if not isinstance(database, list):
            return None

        normalized_script = normalize_spaces(script)

        # نبحث عن أطول النصوص أولًا لتقليل التطابقات الجزئية.
        quran_entries = [
            item for item in database
            if str(item.get("type", "")).upper() == "QURAN"
            and item.get("text")
        ]
        quran_entries.sort(
            key=lambda item: len(
                normalize_spaces(item.get("text", ""))
            ),
            reverse=True,
        )

        for item in quran_entries:
            quran_text = normalize_spaces(
                item.get("text", "")
            )

            if not quran_text:
                continue

            # نستخدم جزءًا من بداية النص حتى لا نفشل
            # بسبب اختلاف علامات التشكيل/الوقف.
            probe = quran_text[:120]

            if (
                probe in normalized_script
                or quran_text in normalized_script
            ):
                return {
                    "type": "QURAN",
                    "source": item.get(
                        "source",
                        "",
                    ),
                    "text": item.get(
                        "text",
                        "",
                    ),
                }

        return None

    except Exception as exc:
        print(
            f"⚠️ تعذر فحص قاعدة القرآن المحلية: {exc}"
        )
        return None


def load_selected_ready_script() -> Tuple[str, List[str], List[Dict[str, str]], int, int]:
    number, script, total_count = choose_random_ready_voiceover()

    # مرجع القرآن يُستخرج من النص نفسه من قاعدة القرآن المحلية.
    references = []

    local_quran = find_quran_reference_from_local_json(
        script
    )

    if local_quran:
        references.append(local_quran)
        print(
            "📖 تم التعرف على آية قرآن داخل النص:"
            f" {local_quran['source']}"
        )

    scenes = build_visual_queries_from_script(
        script,
        SCENE_COUNT,
    )

    build_ready_script_file(
        number=number,
        script=script,
        references=references,
        scenes=scenes,
    )

    print(
        f"🎙️ تم اختيار نص جاهز رقم {number} "
        f"من أصل {total_count} نص."
    )

    print("✅ AI غير مستخدم في كتابة السكريبت.")
    print(
        f"📄 تم حفظ النسخة المختارة في:\n{LATEST_SCRIPT}"
    )

    return (
        script,
        scenes,
        references,
        number,
        total_count,
    )



# ============================================================
# QURAN REFERENCE PARSING
# ============================================================

ARABIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩",
    "0123456789",
)

SURAH_NAMES = {
    "الفاتحة": 1,
    "البقرة": 2,
    "آل عمران": 3,
    "ال عمران": 3,
    "النساء": 4,
    "المائدة": 5,
    "الأنعام": 6,
    "الأعراف": 7,
    "الأنفال": 8,
    "التوبة": 9,
    "يونس": 10,
    "هود": 11,
    "يوسف": 12,
    "الرعد": 13,
    "إبراهيم": 14,
    "ابراهيم": 14,
    "الحجر": 15,
    "النحل": 16,
    "الإسراء": 17,
    "الاسراء": 17,
    "الكهف": 18,
    "مريم": 19,
    "طه": 20,
    "الأنبياء": 21,
    "الانبياء": 21,
    "الحج": 22,
    "المؤمنون": 23,
    "النور": 24,
    "الفرقان": 25,
    "الشعراء": 26,
    "النمل": 27,
    "القصص": 28,
    "العنكبوت": 29,
    "الروم": 30,
    "لقمان": 31,
    "السجدة": 32,
    "الأحزاب": 33,
    "سبأ": 34,
    "فاطر": 35,
    "يس": 36,
    "الصافات": 37,
    "ص": 38,
    "الزمر": 39,
    "غافر": 40,
    "فصلت": 41,
    "الشورى": 42,
    "الزخرف": 43,
    "الدخان": 44,
    "الجاثية": 45,
    "الأحقاف": 46,
    "محمد": 47,
    "الفتح": 48,
    "الحجرات": 49,
    "ق": 50,
    "الذاريات": 51,
    "الطور": 52,
    "النجم": 53,
    "القمر": 54,
    "الرحمن": 55,
    "الواقعة": 56,
    "الحديد": 57,
    "المجادلة": 58,
    "الحشر": 59,
    "الممتحنة": 60,
    "الصف": 61,
    "الجمعة": 62,
    "المنافقون": 63,
    "التغابن": 64,
    "الطلاق": 65,
    "التحريم": 66,
    "الملك": 67,
    "القلم": 68,
    "الحاقة": 69,
    "المعارج": 70,
    "نوح": 71,
    "الجن": 72,
    "المزمل": 73,
    "المدثر": 74,
    "القيامة": 75,
    "الإنسان": 76,
    "الانسان": 76,
    "المرسلات": 77,
    "النبأ": 78,
    "النازعات": 79,
    "عبس": 80,
    "التكوير": 81,
    "الانفطار": 82,
    "المطففين": 83,
    "الانشقاق": 84,
    "البروج": 85,
    "الطارق": 86,
    "الأعلى": 87,
    "الاعلى": 87,
    "الغاشية": 88,
    "الفجر": 89,
    "البلد": 90,
    "الشمس": 91,
    "الليل": 92,
    "الضحى": 93,
    "الشرح": 94,
    "التين": 95,
    "العلق": 96,
    "القدر": 97,
    "البينة": 98,
    "الزلزلة": 99,
    "العاديات": 100,
    "القارعة": 101,
    "التكاثر": 102,
    "العصر": 103,
    "الهمزة": 104,
    "الفيل": 105,
    "قريش": 106,
    "الماعون": 107,
    "الكوثر": 108,
    "الكافرون": 109,
    "النصر": 110,
    "المسد": 111,
    "تبت": 111,
    "الإخلاص": 112,
    "الاخلاص": 112,
    "الفلق": 113,
    "الناس": 114,
}


def digits_to_ascii(value: str) -> str:
    return value.translate(ARABIC_DIGITS)


def parse_quran_reference(source: str) -> Optional[Tuple[int, int, int]]:
    """
    يدعم أمثلة مثل:
    سورة الزمر - آية 53
    سورة نوح - الآيات 10-12
    سورة البقرة - آية 255
    """
    if not source:
        return None

    normalized = digits_to_ascii(source.strip())
    normalized = re.sub(r"[" + "إأآ" + "]", "ا", normalized)

    surah_match = re.search(
        r"سورة\s+(.+?)(?=\s*-\s*(?:الآية|آية|الآيات|آيات)|$)",
        normalized,
    )
    if not surah_match:
        return None

    surah_name = surah_match.group(1).strip()
    surah_name = re.sub(
        r"\s+",
        " ",
        surah_name,
    )

    surah_number = None

    if surah_name.isdigit():
        surah_number = int(surah_name)
    else:
        # أطول أسماء أولًا
        for name, number in sorted(
            SURAH_NAMES.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if surah_name == name:
                surah_number = number
                break

    if not surah_number:
        return None

    ayah_match = re.search(
        r"(?:الآيات|آيات|الآية|آية)\s*"
        r"(\d+)"
        r"(?:\s*[-–]\s*(\d+))?",
        normalized,
    )

    if not ayah_match:
        return None

    start = int(ayah_match.group(1))
    end = (
        int(ayah_match.group(2))
        if ayah_match.group(2)
        else start
    )

    return surah_number, start, end


# ============================================================
# EVERYAYAH
# ============================================================

def download_everyayah_ayah(
    surah: int,
    ayah: int,
    output_dir: Path,
) -> Path:

    surah_str = str(surah).zfill(3)
    ayah_str = str(ayah).zfill(3)

    url = (
        f"https://everyayah.com/data/"
        f"{QURAN_RECITER}/{surah_str}{ayah_str}.mp3"
    )

    output_file = (
        output_dir /
        f"{surah_str}{ayah_str}_{QURAN_RECITER}.mp3"
    )

    if output_file.exists() and output_file.stat().st_size > 0:
        return output_file

    print(
        f"   🎙️ Quran: {surah_str}:{ayah_str}"
    )

    response = requests.get(
        url,
        stream=True,
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()

    with open(output_file, "wb") as f:
        for chunk in response.iter_content(
            chunk_size=1024 * 1024
        ):
            if chunk:
                f.write(chunk)

    if output_file.stat().st_size == 0:
        output_file.unlink(missing_ok=True)
        raise RuntimeError(
            f"EveryAyah حمّل ملفًا فارغًا: {url}"
        )

    return output_file


def build_quran_audio_for_reference(
    reference: Dict[str, str],
) -> List[Path]:

    parsed = parse_quran_reference(
        reference.get("source", "")
    )

    if not parsed:
        raise RuntimeError(
            "تعذر تفسير مرجع القرآن: "
            + reference.get("source", "")
        )

    surah, start, end = parsed
    paths = []

    for ayah in range(start, end + 1):

        paths.append(
            download_everyayah_ayah(
                surah,
                ayah,
                QURAN_AUDIO_DIR,
            )
        )

    return paths


# ============================================================
# SCRIPT SEGMENTATION
# ============================================================

def find_reference_lines(
    script_lines: List[str],
    reference_text: str,
    start_index: int = 0,
) -> Optional[Tuple[int, int]]:

    ref_lines = [
        normalize_spaces(line)
        for line in reference_text.splitlines()
        if line.strip()
    ]

    if not ref_lines:
        return None

    # محاولة مطابقة عدة أسطر متتالية
    max_start = len(script_lines) - len(ref_lines)

    for i in range(
        start_index,
        max_start + 1,
    ):

        candidate = [
            normalize_spaces(x)
            for x in script_lines[
                i:i + len(ref_lines)
            ]
        ]

        if candidate == ref_lines:
            return i, i + len(ref_lines)

    # fallback: تطابق كامل للنص بعد إزالة فواصل السطر
    flattened_ref = normalize_spaces(
        " ".join(ref_lines)
    )

    for i in range(
        start_index,
        len(script_lines),
    ):

        for end in range(
            i + 1,
            min(len(script_lines), i + len(ref_lines) + 4) + 1,
        ):

            candidate = normalize_spaces(
                " ".join(script_lines[i:end])
            )

            if candidate == flattened_ref:
                return i, end

    return None


def build_audio_segments(
    script: str,
    references: List[Dict[str, str]],
) -> List[Dict]:

    script_lines = [
        line.strip()
        for line in script.splitlines()
        if line.strip()
    ]

    quran_refs = [
        ref
        for ref in references
        if ref.get("type", "").upper() == "QURAN"
    ]

    # لو مفيش قرآن: الصوت كله ElevenLabs
    if not quran_refs:
        return [
            {
                "type": "tts",
                "text": "\n".join(script_lines),
            }
        ]

    segments = []
    cursor = 0
    ref_cursor = 0

    while ref_cursor < len(quran_refs):

        ref = quran_refs[ref_cursor]

        match = find_reference_lines(
            script_lines,
            ref["text"],
            start_index=cursor,
        )

        if not match:
            print(
                "⚠️ لم أجد نص الآية داخل SCRIPT "
                f"للمرجع: {ref.get('source')}"
            )
            ref_cursor += 1
            continue

        start, end = match

        before = script_lines[cursor:start]
        if before:
            segments.append(
                {
                    "type": "tts",
                    "text": "\n".join(before),
                }
            )

        segments.append(
            {
                "type": "quran",
                "reference": ref,
            }
        )

        cursor = end
        ref_cursor += 1

    after = script_lines[cursor:]

    if after:
        segments.append(
            {
                "type": "tts",
                "text": "\n".join(after),
            }
        )

    # لو حصل فشل في فصل كل شيء، نحتفظ بالنص كاملًا
    if not any(
        segment["type"] == "quran"
        for segment in segments
    ):
        return [
            {
                "type": "tts",
                "text": "\n".join(script_lines),
            }
        ]

    return segments


# ============================================================
# ELEVENLABS TTS
# ============================================================

def elevenlabs_tts(
    text: str,
    eleven_keys: List[str],
    output_file: Path,
) -> Path:

    text = text.strip()

    if not text:
        raise ValueError("طلب ElevenLabs بنص فارغ.")

    last_error = None

    for index, eleven_key in enumerate(eleven_keys, start=1):

        print(f"   🔑 ElevenLabs key {index}/{len(eleven_keys)}")

        try:
            response = requests.post(
                "https://api.elevenlabs.io/v1/text-to-speech/"
                f"{ELEVENLABS_VOICE_ID}",
                headers={
                    "xi-api-key": eleven_key,
                    "Content-Type": "application/json",
                },
                params={
                    "output_format": ELEVENLABS_FORMAT,
                },
                json={
                    "text": text,
                    "model_id": ELEVENLABS_MODEL,
                },
                timeout=120,
            )

            if response.status_code == 200:
                output_file.write_bytes(response.content)
                if output_file.stat().st_size == 0:
                    raise RuntimeError("ElevenLabs أنشأ ملفًا فارغًا.")
                print("   ✅ ElevenLabs نجح.")
                return output_file

            last_error = (
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
            print(f"   ⚠️ المفتاح {index} فشل: {last_error}")

        except requests.RequestException as exc:
            last_error = str(exc)
            print(f"   ⚠️ خطأ اتصال بالمفتاح {index}: {exc}")

    raise RuntimeError(
        "كل مفاتيح ElevenLabs فشلت.\n"
        f"آخر خطأ: {last_error}"
    )


def create_audio_track(
    script: str,
    references: List[Dict[str, str]],
    eleven_keys: List[str],
    ffmpeg: str,
    ffprobe: str,
) -> Path:

    segments = build_audio_segments(
        script,
        references,
    )

    segment_files = []

    print("\n" + "=" * 70)
    print("STEP 2 — AUDIO")
    print("=" * 70)

    for index, segment in enumerate(
        segments,
        start=1,
    ):

        if segment["type"] == "tts":

            output = (
                AUDIO_DIR /
                f"tts_{index:03d}.mp3"
            )

            print(
                f"\n🗣️ ElevenLabs segment {index}:"
            )
            print(
                segment["text"][:300]
            )

            elevenlabs_tts(
                segment["text"],
                eleven_keys,
                output,
            )

            segment_files.append(
                output
            )

        else:

            print(
                f"\n📖 Quran segment {index}: "
                f"{segment['reference']['source']}"
            )

            quran_files = (
                build_quran_audio_for_reference(
                    segment["reference"]
                )
            )

            segment_files.extend(
                quran_files
            )

    if not segment_files:
        raise RuntimeError(
            "لم يتم إنشاء أي ملف صوتي."
        )

    # concat demuxer
    concat_file = AUDIO_DIR / "audio_concat.txt"

    with open(
        concat_file,
        "w",
        encoding="utf-8",
    ) as f:

        for path in segment_files:
            escaped = str(path).replace(
                "\\",
                "/",
            ).replace(
                "'",
                "'\\''",
            )

            f.write(
                f"file '{escaped}'\n"
            )

    final_audio = WORK_DIR / "final_audio.mp3"

    run_command(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(final_audio),
        ],
        "دمج ElevenLabs + EveryAyah",
    )

    duration = get_duration(
        final_audio,
        ffprobe,
    )

    print(
        f"✅ مدة الصوت النهائي: {duration:.2f} ثانية"
    )

    # تحديث voiceover النهائي لو احتجنا.
    VOICEOVER_TEXT.write_text(
        script.strip(),
        encoding="utf-8",
    )

    return final_audio


# ============================================================
# VISUAL SEARCH
# ============================================================

def search_pixabay_videos(
    keyword: str,
    pixabay_key: str,
) -> List[Dict]:
    response = requests.get(
        "https://pixabay.com/api/videos/",
        params={
            "key": pixabay_key,
            "q": keyword,
            "video_type": "film",
            "safesearch": "true",
            "order": "popular",
            "per_page": RESULTS_PER_QUERY,
        },
        timeout=30,
    )
    response.raise_for_status()

    results = []

    for item in response.json().get(
        "hits",
        [],
    ):

        duration = float(
            item.get("duration", 0)
        )

        if duration < MIN_ASSET_DURATION:
            continue

        selected = None

        for quality in [
            "large",
            "medium",
            "small",
            "tiny",
        ]:

            candidate = item.get(
                "videos",
                {},
            ).get(quality)

            if not candidate:
                continue

            width = int(
                candidate.get("width", 0)
            )
            height = int(
                candidate.get("height", 0)
            )

            if height > width:
                selected = candidate
                break

        if not selected:
            continue

        results.append(
            {
                "source": "pixabay",
                "type": "video",
                "id": item.get("id"),
                "keyword": keyword,
                "duration": duration,
                "width": selected.get("width"),
                "height": selected.get("height"),
                "url": selected.get("url"),
                "page_url": item.get("pageURL"),
            }
        )

    return results


def search_pexels_videos(
    keyword: str,
    pexels_key: str,
) -> List[Dict]:
    response = requests.get(
        "https://api.pexels.com/v1/videos/search",
        headers={
            "Authorization": pexels_key,
        },
        params={
            "query": keyword,
            "orientation": "portrait",
            "size": "medium",
            "per_page": RESULTS_PER_QUERY,
        },
        timeout=30,
    )
    response.raise_for_status()

    results = []

    for item in response.json().get(
        "videos",
        [],
    ):

        width = int(
            item.get("width", 0)
        )
        height = int(
            item.get("height", 0)
        )
        duration = float(
            item.get("duration", 0)
        )

        if height <= width:
            continue

        if duration < MIN_ASSET_DURATION:
            continue

        candidates = []

        for file in item.get(
            "video_files",
            [],
        ):

            fw = int(
                file.get("width", 0)
            )
            fh = int(
                file.get("height", 0)
            )

            if (
                fh > fw
                and file.get("link")
            ):
                candidates.append(file)

        if not candidates:
            continue

        candidates.sort(
            key=lambda x:
                int(x.get("width", 0))
                * int(x.get("height", 0)),
            reverse=True,
        )

        selected = candidates[0]

        results.append(
            {
                "source": "pexels",
                "type": "video",
                "id": item.get("id"),
                "keyword": keyword,
                "duration": duration,
                "width": selected.get("width"),
                "height": selected.get("height"),
                "url": selected.get("link"),
                "page_url": item.get("url"),
            }
        )

    return results


def search_pixabay_photos(
    keyword: str,
    pixabay_key: str,
) -> List[Dict]:
    response = requests.get(
        "https://pixabay.com/api/",
        params={
            "key": pixabay_key,
            "q": keyword,
            "image_type": "photo",
            "orientation": "vertical",
            "safesearch": "true",
            "order": "popular",
            "per_page": RESULTS_PER_QUERY,
        },
        timeout=30,
    )
    response.raise_for_status()

    results = []

    for item in response.json().get(
        "hits",
        [],
    ):

        width = int(
            item.get("imageWidth", 0)
        )
        height = int(
            item.get("imageHeight", 0)
        )

        if height <= width:
            continue

        url = (
            item.get("largeImageURL")
            or item.get("imageURL")
        )

        if not url:
            continue

        results.append(
            {
                "source": "pixabay",
                "type": "photo",
                "id": item.get("id"),
                "keyword": keyword,
                "duration": 0,
                "width": width,
                "height": height,
                "url": url,
                "page_url": item.get("pageURL"),
            }
        )

    return results


def search_pexels_photos(
    keyword: str,
    pexels_key: str,
) -> List[Dict]:
    response = requests.get(
        "https://api.pexels.com/v1/search",
        headers={
            "Authorization": pexels_key,
        },
        params={
            "query": keyword,
            "orientation": "portrait",
            "size": "large",
            "per_page": RESULTS_PER_QUERY,
        },
        timeout=30,
    )
    response.raise_for_status()

    results = []

    for item in response.json().get(
        "photos",
        [],
    ):

        width = int(
            item.get("width", 0)
        )
        height = int(
            item.get("height", 0)
        )

        if height <= width:
            continue

        src = item.get("src", {})

        url = (
            src.get("original")
            or src.get("large2x")
            or src.get("large")
        )

        if not url:
            continue

        results.append(
            {
                "source": "pexels",
                "type": "photo",
                "id": item.get("id"),
                "keyword": keyword,
                "duration": 0,
                "width": width,
                "height": height,
                "url": url,
                "page_url": item.get("url"),
            }
        )

    return results


def remove_duplicates(
    items: List[Dict],
) -> List[Dict]:

    seen = set()
    output = []

    for item in items:

        key = (
            item["source"],
            item["id"],
        )

        if key in seen:
            continue

        seen.add(key)
        output.append(item)

    return output


def choose_scene_asset(
    candidates: List[Dict],
    preferred_type: str,
    scene_duration: float,
    used_ids: set,
    used_sources: set,
) -> Optional[Dict]:

    available = [
        item
        for item in candidates
        if (
            item["source"],
            item["id"],
        ) not in used_ids
    ]

    # أولًا: النوع المفضل
    preferred = [
        item
        for item in available
        if item["type"] == preferred_type
    ]

    if preferred:
        available = preferred + [
            item
            for item in available
            if item not in preferred
        ]

    # نفضل مصدرًا مختلفًا إن أمكن
    diverse_source = [
        item
        for item in available
        if item["source"] not in used_sources
    ]

    if diverse_source:
        available = diverse_source + [
            item
            for item in available
            if item not in diverse_source
        ]

    if not available:
        return None

    # للفيديو: نفضل أن يكون أطول من زمن المشهد
    video_ready = [
        item
        for item in available
        if (
            item["type"] == "video"
            and item["duration"] >= scene_duration
        )
    ]

    if video_ready:
        # الأقرب للمدة المطلوبة
        video_ready.sort(
            key=lambda x:
                abs(x["duration"] - scene_duration)
        )
        return video_ready[0]

    return available[0]


# ============================================================
# VISUAL PREPARATION
# ============================================================

def download_asset(
    item: Dict,
    scene_index: int,
) -> Path:

    ext = ".mp4" if item["type"] == "video" else ".jpg"

    filename = (
        f"scene_{scene_index:02d}_"
        f"{clean_filename(item['keyword'])}_"
        f"{item['source']}_"
        f"{item['id']}{ext}"
    )

    output = ASSET_DIR / filename

    if output.exists() and output.stat().st_size > 0:
        return output

    response = requests.get(
        item["url"],
        stream=True,
        timeout=90,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )
    response.raise_for_status()

    with open(output, "wb") as f:
        for chunk in response.iter_content(
            chunk_size=1024 * 1024
        ):
            if chunk:
                f.write(chunk)

    if output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"تم تنزيل ملف فارغ: {output}"
        )

    return output


def wrap_caption(text: str, max_chars: int = 28) -> str:
    words = normalize_spaces(text).split()
    lines = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if len(candidate) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines[:5])


def render_caption_png(
    magick: str,
    text: str,
    output_file: Path,
    is_quran: bool = False,
) -> None:
    text = wrap_caption(text)
    if not text:
        return

    # نستخدم ImageMagick كما في الاختبار الناجح.
    text_color = "#F6E7B0" if is_quran else "white"
    border_color = "#C9A227" if is_quran else "#FFFFFF"

    caption_path = output_file.parent
    caption_path.mkdir(parents=True, exist_ok=True)

    cmd = [
        magick,
        "-size", f"{VIDEO_WIDTH}x{CAPTION_BOX_HEIGHT}",
        "xc:none",
        "-fill", "rgba(0,0,0,0.62)",
        "-stroke", border_color if is_quran else "rgba(255,255,255,0.12)",
        "-strokewidth", "2",
        "-draw", f"roundrectangle 60,15 1020,{CAPTION_BOX_HEIGHT-15} 35,35",
        "-fill", text_color,
        "-stroke", "none",
        "-font", CAPTION_FONT,
        "-pointsize", str(CAPTION_TEXT_SIZE if not is_quran else 50),
        "-gravity", "center",
        "-annotate", "+0+0", text,
        str(output_file),
    ]

    run_command(cmd, f"إنشاء Caption: {output_file.name}")


def make_video_clip(
    asset_file: Path,
    scene_duration: float,
    output_file: Path,
    ffmpeg: str,
    magick: str,
    caption_text: str = "",
    is_quran: bool = False,
) -> None:
    is_video = asset_file.suffix.lower() == ".mp4"
    duration = max(0.2, scene_duration)
    fade_duration = min(0.25, duration / 4)

    video_filter = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:"
        "force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
        f"fps={VIDEO_FPS},"
        f"fade=t=in:st=0:d={fade_duration},"
        f"fade=t=out:st={max(0, duration-fade_duration)}:d={fade_duration},"
        "setsar=1"
    )

    base_file = output_file.with_name(output_file.stem + "_base.mp4")

    if is_video:
        base_cmd = [
            ffmpeg, "-y", "-stream_loop", "-1", "-i", str(asset_file),
            "-t", f"{duration:.3f}", "-vf", video_filter, "-an",
            "-r", str(VIDEO_FPS), "-c:v", "libx264", "-preset", "medium",
            "-crf", "20", "-pix_fmt", "yuv420p", str(base_file)
        ]
    else:
        base_cmd = [
            ffmpeg, "-y", "-loop", "1", "-i", str(asset_file),
            "-t", f"{duration:.3f}", "-vf", video_filter, "-an",
            "-r", str(VIDEO_FPS), "-c:v", "libx264", "-preset", "medium",
            "-crf", "20", "-pix_fmt", "yuv420p", str(base_file)
        ]

    run_command(base_cmd, f"تجهيز الفيديو الأساسي -> {output_file.name}")

    if not caption_text.strip():
        shutil.move(str(base_file), str(output_file))
        return

    caption_png = output_file.with_suffix(".caption.png")
    render_caption_png(magick, caption_text, caption_png, is_quran=is_quran)

    overlay_cmd = [
        ffmpeg, "-y",
        "-i", str(base_file),
        "-loop", "1", "-i", str(caption_png),
        "-filter_complex",
        "[1:v]format=rgba[cap];[0:v][cap]overlay=0:H-h:enable='between(t,0," + f"{duration:.3f}" + ")'[v]",
        "-map", "[v]",
        "-an",
        "-t", f"{duration:.3f}",
        "-r", str(VIDEO_FPS),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(output_file),
    ]

    run_command(overlay_cmd, f"دمج Caption -> {output_file.name}")
    base_file.unlink(missing_ok=True)
    caption_png.unlink(missing_ok=True)


# ============================================================
# STEP 4 — ASSETS
# ============================================================

def build_scene_caption_texts(
    script: str,
    scene_count: int,
    references: List[Dict[str, str]],
) -> List[Tuple[str, bool]]:
    lines = [x.strip() for x in script.splitlines() if x.strip()]
    if not lines:
        return [("", False)] * scene_count

    # توزيع الجمل على المشاهد حسب عدد الكلمات بدل قص النص عشوائيًا.
    total_words = sum(len(x.split()) for x in lines)
    target = max(1, total_words / scene_count)
    groups = []
    current = []
    words = 0

    for line in lines:
        if len(groups) < scene_count - 1 and current and words >= target:
            groups.append(current)
            current = []
            words = 0
        current.append(line)
        words += len(line.split())
    if current:
        groups.append(current)

    while len(groups) < scene_count:
        groups.append([])

    quran_texts = [normalize_spaces(r.get("text", "")) for r in references if r.get("type", "").upper() == "QURAN"]
    result = []
    for group in groups[:scene_count]:
        caption = "\n".join(group)
        normalized_group = normalize_spaces(caption)
        is_quran = any(q and q in normalized_group for q in quran_texts)
        result.append((caption, is_quran))
    return result


def build_visual_track(
    scene_descriptions: List[str],
    audio_duration: float,
    script: str,
    references: List[Dict[str, str]],
    pixabay_key: str,
    pexels_key: str,
    ffmpeg: str,
    magick: str,
) -> List[Path]:

    print("\n" + "=" * 70)
    print("STEP 4 — VISUALS")
    print("=" * 70)

    if not scene_descriptions:
        raise RuntimeError(
            "لا توجد SCENE descriptions."
        )

    # نستخدم 8 مشاهد أو العدد الموجود فعليًا
    scenes = scene_descriptions[:SCENE_COUNT]
    caption_data = build_scene_caption_texts(
        script,
        len(scenes),
        references,
    )

    # تقسيم مدة الصوت بالتساوي مبدئيًا.
    # هذا أبسط وأكثر استقرارًا للفيديو الأول.
    scene_duration = audio_duration / len(scenes)

    all_candidates = []
    used_ids = set()
    used_sources = set()
    selected = []

    for index, scene in enumerate(
        scenes,
        start=1,
    ):

        query = normalize_spaces(scene)

        print(
            f"\n🔎 Scene {index}: {query}"
        )

        candidates = []

        # Pixabay
        try:
            candidates.extend(
                search_pixabay_videos(
                    query,
                    pixabay_key,
                )
            )
        except Exception as e:
            print(
                f"   ⚠️ Pixabay video: {e}"
            )

        try:
            candidates.extend(
                search_pixabay_photos(
                    query,
                    pixabay_key,
                )
            )
        except Exception as e:
            print(
                f"   ⚠️ Pixabay photo: {e}"
            )

        # Pexels
        try:
            candidates.extend(
                search_pexels_videos(
                    query,
                    pexels_key,
                )
            )
        except Exception as e:
            print(
                f"   ⚠️ Pexels video: {e}"
            )

        try:
            candidates.extend(
                search_pexels_photos(
                    query,
                    pexels_key,
                )
            )
        except Exception as e:
            print(
                f"   ⚠️ Pexels photo: {e}"
            )

        candidates = remove_duplicates(
            candidates
        )

        if not candidates:
            print(
                "   ❌ لم نجد خامة مباشرة."
            )
            continue

        preferred_type = SCENE_PREFERENCES[
            (index - 1) % len(SCENE_PREFERENCES)
        ]

        chosen = choose_scene_asset(
            candidates,
            preferred_type,
            scene_duration,
            used_ids,
            used_sources,
        )

        if not chosen:
            print(
                "   ❌ لم نستطع اختيار خامة."
            )
            continue

        used_ids.add(
            (
                chosen["source"],
                chosen["id"],
            )
        )
        used_sources.add(
            chosen["source"]
        )

        print(
            f"   ✅ اختيار: "
            f"{chosen['source']} / "
            f"{chosen['type']} / "
            f"{chosen['id']}"
        )

        try:

            asset_file = download_asset(
                chosen,
                index,
            )

            clip_file = (
                CLIP_DIR /
                f"scene_{index:02d}.mp4"
            )

            caption_text, is_quran_caption = caption_data[index - 1]
            make_video_clip(
                asset_file,
                scene_duration,
                clip_file,
                ffmpeg,
                magick,
                caption_text=caption_text,
                is_quran=is_quran_caption,
            )

            selected.append(
                clip_file
            )

        except Exception as e:

            print(
                f"   ❌ فشل تجهيز Scene {index}: {e}"
            )

    if not selected:
        raise RuntimeError(
            "لم يتمكن البرنامج من تجهيز أي خامات بصرية."
        )

    return selected


# ============================================================
# STEP 5 — FINAL VIDEO
# ============================================================

def concat_video_clips(
    clip_files: List[Path],
    output_file: Path,
    ffmpeg: str,
) -> None:

    concat_file = CLIP_DIR / "video_concat.txt"

    with open(
        concat_file,
        "w",
        encoding="utf-8",
    ) as f:

        for path in clip_files:

            escaped = str(path).replace(
                "\\",
                "/",
            ).replace(
                "'",
                "'\\''",
            )

            f.write(
                f"file '{escaped}'\n"
            )

    video_only = WORK_DIR / "visual_track.mp4"

    run_command(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-r",
            str(VIDEO_FPS),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(video_only),
        ],
        "دمج جميع المشاهد",
    )

    return video_only


def mux_audio_video(
    video_file: Path,
    audio_file: Path,
    output_file: Path,
    ffmpeg: str,
) -> None:

    run_command(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_file),
            "-i",
            str(audio_file),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_file),
        ],
        "إضافة الصوت النهائي للفيديو",
    )



# ============================================================
# YOUTUBE UPLOAD
# ============================================================

YT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

YOUTUBE_PRIVACY = os.getenv("YOUTUBE_PRIVACY", "public").strip().lower()
YOUTUBE_CHECK_MINUTES = int(os.getenv("YOUTUBE_CHECK_MINUTES", "10"))

if YOUTUBE_PRIVACY not in {"public", "private", "unlisted"}:
    raise ValueError(
        "YOUTUBE_PRIVACY must be one of: public, private, unlisted"
    )


def ensure_youtube_token_file() -> None:
    """Create token.json from the GitHub/local environment when available."""
    token_json = os.getenv("YOUTUBE_TOKEN_JSON", "").strip()
    if token_json:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token_json, encoding="utf-8")
        print("✅ YouTube token loaded from YOUTUBE_TOKEN_JSON.")


def get_youtube_client():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    ensure_youtube_token_file()

    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"YouTube token not found:\n{TOKEN_FILE}\n"
            "Set YOUTUBE_TOKEN_JSON or provide token.json locally."
        )

    credentials = Credentials.from_authorized_user_file(
        str(TOKEN_FILE),
        YT_SCOPES,
    )

    if credentials.expired and credentials.refresh_token:
        print("🔄 YouTube token expired -> refreshing...")
        credentials.refresh(Request())
        TOKEN_FILE.write_text(
            credentials.to_json(),
            encoding="utf-8",
        )
        print("✅ YouTube token refreshed.")

    if not credentials.valid:
        raise RuntimeError(
            "YouTube token is invalid. Run login_channel.py again."
        )

    granted = set(credentials.scopes or [])
    missing = [scope for scope in YT_SCOPES if scope not in granted]
    if missing:
        raise RuntimeError(
            "YouTube token is missing required scopes:\n" +
            "\n".join(missing)
        )

    return build("youtube", "v3", credentials=credentials)


def build_youtube_metadata(script: str, selected_number: int):
    lines = [
        line.strip()
        for line in script.splitlines()
        if line.strip()
    ]

    # Use the first spoken line as a stable, deterministic title source.
    hook = lines[0] if lines else "Daily Islamic Reminder"
    hook = re.sub(r"[\\/:*?\"<>|]", "", hook)
    hook = normalize_spaces(hook)
    if len(hook) > 70:
        hook = hook[:70].rsplit(" ", 1)[0]

    title = f"{hook} #Shorts"
    if len(title) > 100:
        title = title[:100]

    description = (
        "Daily Islamic reminder from the نور الدقيقة channel.\n\n"
        "Qur'an and authentic Sunnah reminders in a short video format.\n\n"
        "#Shorts #Islam #Quran #Hadith #IslamicReminder"
    )

    tags = [
        "Islamic reminder",
        "Islam",
        "Quran",
        "Hadith",
        "Islamic Shorts",
        "Muslim reminder",
        "نور الدقيقة",
    ]

    return {
        "title": title,
        "description": description,
        "tags": tags,
    }


def upload_to_youtube(video_path: Path, script: str, selected_number: int):
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    youtube = get_youtube_client()
    metadata = build_youtube_metadata(script, selected_number)

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": "27",
            "defaultLanguage": "ar",
            "defaultAudioLanguage": "ar",
        },
        "status": {
            "privacyStatus": YOUTUBE_PRIVACY,
            "selfDeclaredMadeForKids": False,
        },
    }

    print("\n" + "=" * 70)
    print("STEP 6 — YOUTUBE UPLOAD")
    print("=" * 70)
    print(f"📤 Title: {metadata['title']}")
    print("⬆️ Uploading video...")

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=5 * 1024 * 1024,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    video_id = None

    while True:
        status, response = request.next_chunk()

        if status:
            print(
                f"⬆️ Upload progress: "
                f"{int(status.progress() * 100)}%",
                flush=True,
            )

        if response:
            video_id = response["id"]
            break

    print(f"✅ Upload completed: {video_id}")

    if YOUTUBE_PRIVACY != "private":
        return video_id

    print(
        f"⏳ Waiting {YOUTUBE_CHECK_MINUTES} minutes before status check..."
    )
    time.sleep(YOUTUBE_CHECK_MINUTES * 60)

    print("🔍 Checking YouTube processing status...")
    try:
        response = (
            youtube.videos()
            .list(part="status", id=video_id)
            .execute()
        )
    except HttpError as error:
        # The upload succeeded. Do not pretend it failed just because a
        # post-upload verification request failed.
        print(f"⚠️ Status check failed after successful upload: {error}")
        print("⚠️ Video remains on YouTube and will stay private.")
        raise RuntimeError(
            f"Upload succeeded with video ID {video_id}, "
            f"but status verification failed: {error}"
        )

    items = response.get("items", [])
    if not items:
        raise RuntimeError(
            f"Upload succeeded with video ID {video_id}, but YouTube "
            "returned no status item."
        )

    status = items[0].get("status", {})
    upload_status = status.get("uploadStatus")
    processing_status = status.get("healthStatus", {}).get("status")

    print(f"📌 Upload status: {upload_status}")

    if upload_status == "failed":
        raise RuntimeError(
            f"YouTube reported upload failure for video {video_id}."
        )

    youtube.videos().update(
        part="status",
        body={
            "id": video_id,
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        },
    ).execute()

    print("🌍 Video is now PUBLIC.")
    print(f"🔗 https://www.youtube.com/watch?v={video_id}")

    return video_id


def cleanup_channel_temp_files():
    print("🗑️ Cleaning channel temporary files...")

    if WORK_DIR.exists():
        shutil.rmtree(
            WORK_DIR,
            ignore_errors=True,
        )

    WORK_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    for folder in [AUDIO_DIR, QURAN_AUDIO_DIR, ASSET_DIR, CLIP_DIR]:
        folder.mkdir(
            parents=True,
            exist_ok=True,
        )

    print("✅ Temporary build data cleaned.")

# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("نور الدقيقة — READY VOICEOVER PIPELINE")
    print("=" * 70)

    ffmpeg = require_binary("ffmpeg")
    ffprobe = require_binary("ffprobe")
    magick = require_magick()

    mode = "EXE" if getattr(sys, "frozen", False) else "PYTHON"
    print(f"🧩 التشغيل: {mode}")
    print(f"📂 مجلد البرنامج: {BASE_DIR}")
    print(f"🛠️ FFmpeg: {ffmpeg}")
    print(f"🛠️ FFprobe: {ffprobe}")
    print(f"🛠️ ImageMagick: {magick}")
    print(f"🔐 GitHub/Environment secrets: {'enabled' if os.getenv('ELEVENLABS_API_KEY') or os.getenv('PIXABAY_API_KEY') or os.getenv('PEXELS_API_KEY') or os.getenv('YOUTUBE_TOKEN_JSON') else 'local files'}")
    print(f"📤 Channel output: {CHANNEL_OUTPUT_DIR}")
    print(f"📝 Caption font: {CAPTION_FONT}")
    print(f"🌍 YouTube privacy: {YOUTUBE_PRIVACY}")

    eleven_keys = read_secret_keys(
        ELEVENLABS_KEY_FILE,
        "ELEVENLABS_API_KEY",
    )
    print(f"🔑 ElevenLabs keys: {len(eleven_keys)}")

    pixabay_key = read_secret(
        PIXABAY_KEY_FILE,
        "PIXABAY_API_KEY",
    )

    pexels_key = read_secret(
        PEXELS_KEY_FILE,
        "PEXELS_API_KEY",
    )

    # --------------------------------------------------------
    # 1 — READY VOICEOVER
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("STEP 1 — READY VOICEOVER")
    print("=" * 70)

    (
        script,
        scenes,
        references,
        selected_number,
        total_scripts,
    ) = load_selected_ready_script()

    quran_count = sum(
        1
        for ref in references
        if ref.get("type", "").upper() == "QURAN"
    )

    print(
        f"✅ عدد المراجع القرآنية في النص: {quran_count}"
    )

    # --------------------------------------------------------
    # 2 — AUDIO
    # --------------------------------------------------------

    final_audio = create_audio_track(
        script=script,
        references=references,
        eleven_keys=eleven_keys,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )

    audio_duration = get_duration(
        final_audio,
        ffprobe,
    )

    # --------------------------------------------------------
    # 3 + 4 — VISUALS
    # --------------------------------------------------------

    clip_files = build_visual_track(
        scene_descriptions=scenes,
        audio_duration=audio_duration,
        script=script,
        references=references,
        pixabay_key=pixabay_key,
        pexels_key=pexels_key,
        ffmpeg=ffmpeg,
        magick=magick,
    )

    # --------------------------------------------------------
    # 5 — FINAL VIDEO
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("STEP 5 — FINAL VIDEO")
    print("=" * 70)

    visual_track = concat_video_clips(
        clip_files,
        WORK_DIR / "visual_track.mp4",
        ffmpeg,
    )

    output_folder = CHANNEL_OUTPUT_DIR
    output_folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    final_output = (
        output_folder
        / f"religious_quotes_{selected_number}_"
          f"{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    )

    mux_audio_video(
        visual_track,
        final_audio,
        final_output,
        ffmpeg,
    )

    final_duration = get_duration(
        final_output,
        ffprobe,
    )

    print("\n" + "=" * 70)
    print("🎉 VIDEO READY")
    print("=" * 70)

    print(
        f"\n🎙️ النص المختار: {selected_number}/{total_scripts}"
    )

    print(
        f"\n📁 الفيديو النهائي:\n{final_output}"
    )

    print(
        f"\n⏱️ المدة: {final_duration:.2f} ثانية"
    )

    print(
        "\n✅ لا يوجد توليد AI للسكريبت."
    )

    print(
        "✅ النص المختار مأخوذ مباشرة من voiceover_scripts.py."
    )

    print(
        "✅ الكابشن مدموج داخل الفيديو باستخدام ImageMagick + FFmpeg."
    )

    print(
        "✅ فيديوهات Pixabay/Pexels بدون صوت."
    )

    print(
        "📱 الإخراج: Portrait 1080x1920."
    )

    # --------------------------------------------------------
    # 6 — YOUTUBE
    # --------------------------------------------------------
    video_id = upload_to_youtube(
        final_output,
        script,
        selected_number,
    )

    # Mark the ready script as used only after a successful public upload.
    save_used_voiceover(selected_number)

    # Keep final output + history/used markers; remove only temp build data.
    cleanup_channel_temp_files()

    print("\n" + "=" * 70)
    print("🎉 CHANNEL PIPELINE COMPLETED SUCCESSFULLY")
    print("=" * 70)
    print(f"📺 Video ID: {video_id}")
    print(f"📁 Final video kept at: {final_output}")




if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⛔ تم الإيقاف بواسطة المستخدم.")
        raise SystemExit(1)
    except Exception as exc:
        print("\n❌ ERROR")
        print(type(exc).__name__, ":", exc)
        raise SystemExit(1)

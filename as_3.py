# -*- coding: utf-8 -*-
"""
Pharma API > salesaptek - pharmacies_a > Google Sheets
Версия: 2025-12-05
 + при создании google таблицы максимальное количество строк увеличено до 50000, если листы существуют то они автоматически расширяются до этого количества строк
 + tweaks (время выполнения, фильтр or_inns, пропуск пустых адресов, без трогания заголовков, принудительный экспорт из колонки A)
 + разделение результатов:
    found == 0 > лист _непривязавшиеся
    found == 1 > основной лист
    found > 1 > лист _неоднозначности (не идёт в основной)
    добавлены таймауты подключения Оракла что бы избежать зависания запросов
    
"""

import os
import re
import json
import logging
import unicodedata
from pathlib import Path
from datetime import datetime
import socket
import ctypes
from ctypes import RTLD_GLOBAL
import time  # ?

import gspread
from gspread.exceptions import WorksheetNotFound
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from openai import OpenAI
import oracledb
import concurrent.futures

from app_config import load_config

_settings = load_config()
ORACLE_USER = _settings["ORACLE_USER"]
ORACLE_PASSWORD = _settings["ORACLE_PASSWORD"]
ORACLE_DSN = _settings["ORACLE_DSN"]
OPENAI_API_KEY = _settings["OPENAI_API_KEY"]
GSERVICE_JSON = _settings["GSERVICE_JSON"]
GSHEET_ID = _settings["GSHEET_ID"]
WORKSHEET_NAME_MAIN = _settings["WORKSHEET_NAME_MAIN"]
WORKSHEET_NAME_UNMATCH = _settings["WORKSHEET_NAME_UNMATCH"]
WORKSHEET_NAME_AMBIG = _settings["WORKSHEET_NAME_AMBIG"]

GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"

# =========================
# НАСТРОЙКИ (.env и .req загружаются в app_config.py)
# =========================

# Oracle

ORACLE_CLIENT = os.environ.get("ORACLE_CLIENT", "/opt/oracle/instantclient")
ORACLE_CLIENT_CONFIG = os.environ.get("ORACLE_CLIENT_CONFIG", str(Path(ORACLE_CLIENT) / "network/admin"))
ORACLE_FORCE_THICK = True
BATCH_LIMIT = 5700
# Oracle timeouts (ms)
ORACLE_CALL_TIMEOUT_MS = 30000  # 30 секунд на любой вызов к Oracle
ORACLE_CONNECT_TIMEOUT_SEC = 20   # максимальное время ожидания connect_oracle()



# OpenAI настройки
OPENAI_TIMEOUT_SECONDS = 25
OPENAI_MAX_RETRIES = 3
OPENAI_BACKOFF_BASE = 1.5

# Логи
LOG_DIR = Path(os.environ.get("APP_LOG_DIR", Path(__file__).parent))
LOG_DIR.mkdir(parents=True, exist_ok=True)
APP_LOG_FILE = LOG_DIR / "pharma_match.log"
SQL_LOG_FILE = LOG_DIR / "sql_debug.log"

# Таймауты
socket.setdefaulttimeout(25)


# =========================
# Загрузка salesaptek
# =========================
def fetch_salesaptek_batch(conn, limit: int):
    # не NULL и есть хотя бы одна цифра в or_inns
    q = r"""
      SELECT *
        FROM (
          SELECT id, or_clientname, or_address, or_inns
            FROM salesaptek
           WHERE 
             active=1
             AND aptek=0
             AND expert=299
             AND or_address IS NOT NULL
             AND or_inns IS NOT NULL
             AND REGEXP_LIKE(or_inns, '\d')
             
             
           ORDER BY id DESC
        )
       WHERE ROWNUM <= :lim
    """
    
    print("===== FETCH SQL =====")
    print(q)
    print("======================")
    
    with conn.cursor() as cur:
        cur.call_timeout = ORACLE_CALL_TIMEOUT_MS  # <-- таймаут запроса
        cur.execute(q, {"lim": limit})
        rows = cur.fetchall()
    items = [{"id": r[0], "name": r[1], "or_address": r[2], "or_inns": r[3]} for r in rows]
    logger.info("Загружено из salesaptek: %s (лимит %s)", len(items), limit)
    return items

# =========================
# ЛОГГЕРЫ
# =========================
logger = logging.getLogger("pharma_match")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(APP_LOG_FILE, encoding="utf-8")
    ch = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)

logger_sql = logging.getLogger("sql_debug")
logger_sql.setLevel(logging.DEBUG)
if not logger_sql.handlers:
    fh_sql = logging.FileHandler(SQL_LOG_FILE, encoding="utf-8")
    fh_sql.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger_sql.addHandler(fh_sql)

print(f"SQL log file: {SQL_LOG_FILE}")
logger.info("SQL log path: %s", SQL_LOG_FILE)

# =========================
# Oracle init/connect
# =========================
def _init_oracle_thick_if_needed():
    ic = Path(ORACLE_CLIENT)
    if not ic.exists():
        raise RuntimeError(f"ORACLE_CLIENT не найден: {ic}")

    os.environ["LD_LIBRARY_PATH"] = f"{ic}:/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu:" + os.environ.get("LD_LIBRARY_PATH","")
    for lib in [
        "/usr/lib/x86_64-linux-gnu/libaio.so.1",
        "/usr/lib/x86_64-linux-gnu/libaio.so.1t64",
        "/usr/lib/x86_64-linux-gnu/libnsl.so.2",
        str(ic / "libclntsh.so"),
    ]:
        try:
            ctypes.CDLL(lib, mode=RTLD_GLOBAL)
        except OSError:
            pass

    if Path(ORACLE_CLIENT_CONFIG).is_dir():
        oracledb.init_oracle_client(lib_dir=str(ic), config_dir=str(ORACLE_CLIENT_CONFIG))
    else:
        oracledb.init_oracle_client(lib_dir=str(ic))
    logger.info("Oracle thick initialized; thin=%s", oracledb.is_thin_mode())

def connect_oracle():
    print("STEP 2/4: Oracle > подключение")
    if ORACLE_FORCE_THICK:
        _init_oracle_thick_if_needed()
        return oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)
    try:
        print("  -> thin...")
        return oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)
    except oracledb.NotSupportedError:
        logger.error("DPY-3010 (старый сервер) > включаем thick")
        _init_oracle_thick_if_needed()
        return oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)
        
def connect_oracle_with_timeout(timeout_sec: int = ORACLE_CONNECT_TIMEOUT_SEC):
    """
    Вызывает connect_oracle() в отдельном потоке и ждёт не дольше timeout_sec.
    Если connect завис (сетевые проблемы, зависший listener), получаем TimeoutError.
    """
    def _do_connect():
        return connect_oracle()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_do_connect)
        return future.result(timeout=timeout_sec)


# =========================
# Google Sheets
# =========================
def ensure_min_grid(ws, min_rows=10_000, min_cols=20):
    """Если лист меньше заданного минимума — расширяем сетку."""
    try:
        if ws.row_count < min_rows:
            ws.add_rows(min_rows - ws.row_count)
        if ws.col_count < min_cols:
            ws.add_cols(min_cols - ws.col_count)
    except Exception:
        ws.resize(rows=max(ws.row_count, min_rows), cols=max(ws.col_count, min_cols))

def get_worksheets_pair():
    """
    Если листы уже существуют — ничего не очищаем и не трогаем заголовки.
    Создаём листы сразу с большим количеством строк/колонок.
    Для существующих листов — гарантируем минимум строк/колонок.
    """
    print("STEP 1/4: Google Sheets > авторизация и открытие листов")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if not Path(GSERVICE_JSON).exists():
        raise RuntimeError(f"GSERVICE_JSON не найден: {GSERVICE_JSON}")
    creds = Credentials.from_service_account_file(GSERVICE_JSON, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GSHEET_ID)
    print(f"  Таблица: {sh.title}")

    header_main = ["id", "name", "address", "inn","confidence","verdict"]
    header_unmatch = ["salesaptek.id", "name", "address", "inn",
                      "region", "city", "street", "house", "etc"]
    header_ambig = ["salesaptek.id", "name", "address", "inn"]

    # --- основной лист ---
    try:
        ws_main = sh.worksheet(WORKSHEET_NAME_MAIN)
        print(f"  Лист найден: {WORKSHEET_NAME_MAIN} (заголовки не трогаем)")
        ensure_min_grid(ws_main, min_rows=10_000, min_cols=20)
    except WorksheetNotFound:
        ws_main = sh.add_worksheet(title=WORKSHEET_NAME_MAIN, rows=10_000, cols=20)
        append_from_col_A(ws_main, [header_main])
        print(f"  Создан лист: {WORKSHEET_NAME_MAIN} (10k?20) + добавлены заголовки")

    # --- лист непривязавшихся ---
    try:
        ws_unmatch = sh.worksheet(WORKSHEET_NAME_UNMATCH)
        print(f"  Лист найден: {WORKSHEET_NAME_UNMATCH} (заголовки не трогаем)")
        ensure_min_grid(ws_unmatch, min_rows=10_000, min_cols=20)
    except WorksheetNotFound:
        ws_unmatch = sh.add_worksheet(title=WORKSHEET_NAME_UNMATCH, rows=20_000, cols=20)
        append_from_col_A(ws_unmatch, [header_unmatch])
        print(f"  Создан лист: {WORKSHEET_NAME_UNMATCH} (10k?20) + добавлены заголовки")

    # --- лист неоднозначностей ---
    try:
        ws_ambig = sh.worksheet(WORKSHEET_NAME_AMBIG)
        print(f"  Лист найден: {WORKSHEET_NAME_AMBIG} (заголовки не трогаем)")
        ensure_min_grid(ws_ambig, min_rows=10_000, min_cols=20)
    except WorksheetNotFound:
        ws_ambig = sh.add_worksheet(title=WORKSHEET_NAME_AMBIG, rows=10_000, cols=20)
        append_from_col_A(ws_ambig, [header_ambig])
        print(f"  Создан лист: {WORKSHEET_NAME_AMBIG} (10k?20) + добавлены заголовки")

    print("  ? Листы готовы к записи (append из колонки A)")
    return ws_main, ws_unmatch, ws_ambig


# =========================
# ХЕЛПЕРЫ
# =========================
def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        if not v:
            return ""
        return json.dumps(v, ensure_ascii=False)
    try:
        return str(v)
    except Exception:
        return ""

def _format_elapsed(seconds: float) -> str:
    msec = int((seconds - int(seconds)) * 1000)
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}.{msec:03d}"

GOOGLE_SHEETS_MAX_RETRIES = 6
GOOGLE_SHEETS_BACKOFF_SECONDS = (5, 10, 20, 40, 60)


def _is_retryable_sheets_error(exc: Exception) -> bool:
    """Определяет временные ошибки Google Sheets/сетевого соединения."""
    message = str(exc).lower()
    retry_markers = (
        "500", "502", "503", "504", "internal error",
        "server error", "bad gateway", "service unavailable",
        "timed out", "timeout", "connection reset",
        "connection aborted", "temporarily unavailable",
    )
    # APIError от gspread может содержать как JSON-ответ, так и HTML-страницу Google.
    return any(marker in message for marker in retry_markers)


def _sheets_request_with_retry(operation_name: str, func):
    """Повторяет временно неудавшийся запрос с экспоненциальной задержкой."""
    for attempt in range(1, GOOGLE_SHEETS_MAX_RETRIES + 1):
        try:
            return func()
        except Exception as exc:
            if attempt >= GOOGLE_SHEETS_MAX_RETRIES or not _is_retryable_sheets_error(exc):
                logger.exception(
                    "Google Sheets: операция '%s' окончательно завершилась ошибкой "
                    "(попытка %s/%s)",
                    operation_name, attempt, GOOGLE_SHEETS_MAX_RETRIES,
                )
                raise

            delay = GOOGLE_SHEETS_BACKOFF_SECONDS[
                min(attempt - 1, len(GOOGLE_SHEETS_BACKOFF_SECONDS) - 1)
            ]
            logger.warning(
                "Google Sheets: временная ошибка в операции '%s' "
                "(попытка %s/%s): %s. Повтор через %s сек.",
                operation_name, attempt, GOOGLE_SHEETS_MAX_RETRIES, exc, delay,
            )
            time.sleep(delay)


def _next_empty_row(ws) -> int:
    """
    Возвращает индекс следующей пустой строки, ориентируясь на колонку A.
    Колонка A — наш «маяк», чтобы точно писать с A.
    """
    col_a = _sheets_request_with_retry(
        f"чтение колонки A листа {ws.title}",
        lambda: ws.col_values(1),
    )
    return len(col_a) + 1


def append_from_col_A(ws, rows):
    """
    Записывает данные строго из колонки A.
    При неопределённом результате запроса повторно использует тот же диапазон:
    повторная запись в него перезаписывает те же значения, а не добавляет дубликат.
    """
    if not rows:
        return

    start_row = _next_empty_row(ws)
    end_row = start_row + len(rows) - 1
    end_col = max(len(row) for row in rows)
    start_a1 = rowcol_to_a1(start_row, 1)
    end_a1 = rowcol_to_a1(end_row, end_col)
    rng = f"{start_a1}:{end_a1}"

    # Приводим строки к одинаковой ширине диапазона.
    normalized_rows = [
        list(row) + [""] * (end_col - len(row))
        for row in rows
    ]

    def write_same_range():
        return ws.update(
            range_name=rng,
            values=normalized_rows,
            value_input_option="RAW",
        )

    _sheets_request_with_retry(
        f"запись диапазона {ws.title}!{rng}",
        write_same_range,
    )



def _extract_confidence_from_modes(modes) -> int | None: #экспорт достоверности в гуглшит
    """
    modes: множество строк вида 'strict_10', 'heur_swap_9', 'not_found'
    Возвращает максимальную числовую достоверность или None, если не нашли.
    """
    max_conf = None
    for m in modes:
        if not m:
            continue
        m_str = str(m)
        m_match = re.search(r"_(\d+)$", m_str)
        if not m_match:
            continue
        c = int(m_match.group(1))
        if max_conf is None or c > max_conf:
            max_conf = c
    return max_conf

def normalize_yo_text(s: str) -> str: #приведение ё к е
    """
    Приводит ё/Ё -> е/Е, остальное не трогает.
    """
    if not s:
        return s
    return s.replace("ё", "е").replace("Ё", "Е")

# =========================
# НОРМАЛИЗАЦИЯ
# =========================
_STREET_TYPE_PAT = re.compile(
    r"""(?i)\b(?:         
        ул\.?|улица|
        просп\.?|пр-?кт|пр-?т|проспект|пр\.?|
        шоссе|ш\.?|
        переулок|пер\.?|
        проезд|пр-?д|
        бульвар|б-?р|бул\.?|
        площадь|пл\.?|
        наб\.?|
        тракт|
        аллея|
        квартал|кв-?л|
        линия|лин\.?|
        тупик|туп\.?|
        просек|прос\.?|
        проулок|пр-?лк|пер|
        сквер|
        мост|
        кольцо|
        имени|им\.?|
        ряд
    )\b""",
    re.VERBOSE
)
_IMENI_PREFIX_PATTERNS = [
    re.compile(r'^(им\.?|имени)\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.\s*', re.IGNORECASE),
    re.compile(r'^(им\.?|имени)\s+', re.IGNORECASE),
    re.compile(r'^[А-ЯЁ]\.\s*[А-ЯЁ]\.\s*'),
    re.compile(r'^[А-ЯЁ]\.\s*'),
]

def normalize_street_name(street: str) -> str:
    """
    Преобразует название улицы:
      - удаляет тип (ул/просп/ш./пер./…), «им./имени» и инициалы,
      - заменяет точки, запятые и кавычки на пробелы,
      - группы дефисов ( -, –, — ) -> '%',
      - убирает пробелы вокруг '%', схлопывает повторные '%',
      - "Микрорайон"/варианты > "мкр",
      - сокращает лишние пробелы.
    """
    if not street:
        return street

    s = unicodedata.normalize("NFKC", street).strip()
    s = normalize_yo_text(s)  # <<< приводим ё к е
    s = _STREET_TYPE_PAT.sub("", s)
    for pat in _IMENI_PREFIX_PATTERNS:
        s = pat.sub("", s)

    # пунктуация -> пробел
    s = re.sub(r"[.,\"'`«»“”‘’]+", " ", s)
    # дефисы -> %
    s = re.sub(r"[-–—]+", "%", s)
    # убрать пробелы вокруг %
    s = re.sub(r"\s*%\s*", "%", s)
    # схлопнуть %%
    s = re.sub(r"%{2,}", "%", s)

    # === нормализация микрорайонов ===
    # "Микрорайон" -> "мкр"
    s = re.sub(r"(?i)\bмикрорайон\b", "мкр", s)
    # варианты с '%' внутри
    s = re.sub(r"(?i)\bмик%он\b", "мкр", s)
    s = re.sub(r"(?i)\bмикр%он\b", "мкр", s)

    # финальная чистка пробелов
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def normalize_house_number(house: str) -> str:
    if not house:
        return ""
    m = re.search(r"\d+", str(house))
    if not m:
        return ""
    try:
        return str(int(m.group(0)))
    except ValueError:
        return m.group(0)

def street_to_like_token(s: str) -> str:
    if not s:
        return ""
    s2 = re.sub(r"-+", "%", s)
    s2 = re.sub(r"\s{2,}", " ", s2).strip()
    return s2

# =========================
# OpenAI — парсинг адреса
# =========================
def gpt_parse_address(or_address: str) -> dict:
    try:
        import httpx
        import httpcore
    except Exception:
        httpx = None
        httpcore = None

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_SECONDS)

    system_msg = (
        "Ты — эксперт по адресам России. "
        "Разбираешь произвольный текст адреса и возвращаешь JSON с полями region, city, street, house, etc. "
        "region — одно слово, отражающее фактическое название региона/области/края (например: 'Алтай', 'Смоленская', 'Краснодарский'). "
        "Не добавляй слова 'область', 'район', 'край' и т.д. "
        "city — город или населённый пункт. ТОЛЬКО название без типа (НЕ писать: г. , п. , с."
        "street — ТОЛЬКО название без типа (НЕ писать: ул, улица, проспект, пр-кт, пр-т, просп., шоссе, ш., переулок, пер., пр-д, б-р, бул., бульвар, пл., площадь, наб., набережная, тракт, аллея, кв-л, квартал, линия, туп., тупик, имени, им. аптека и т.п.). "
        "house — ТОЛЬКО число без литер, букв, дробей и слэшей (например: из '142/2' вернуть '142', из '23б' — '23'). "
        "etc — всё остальное, что не относится к региону, городу, названию улицы или дому. "
        "Не включай индекс и помещение. Возвращай только JSON."
    )
    user_msg = f"Разбери адрес и верни только JSON.\nАдрес: {or_address}"

    retry_errors = tuple()
    if 'httpx' in locals() and httpx:
        retry_errors += (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError)
    if 'httpcore' in locals() and httpcore:
        retry_errors += (httpcore.ReadTimeout, httpcore.ConnectError, httpcore.WriteError, httpcore.ProtocolError)

    last_err = None
    for attempt in range(1, OPENAI_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":system_msg},{"role":"user","content":user_msg}],
                response_format={"type":"json_object"},
            )
            parsed = json.loads(resp.choices[0].message.content)
            parsed = {
                "region": _cell_str(parsed.get("region","")),
                "city":   _cell_str(parsed.get("city","")),
                "street": _cell_str(parsed.get("street","")),
                "house":  _cell_str(parsed.get("house","")),
                "etc":    _cell_str(parsed.get("etc","")),
            }
            logger.info(
                "Parsed (RAW): region=%s | city=%s | street=%s | house=%s | etc=%s",
                parsed.get("region",""),parsed.get("city",""),parsed.get("street",""),
                parsed.get("house",""),parsed.get("etc","")
            )
            return parsed

        except retry_errors as e:
            last_err = e
            logger.warning("OpenAI timeout/сет. ошибка (попытка %s/%s): %s", attempt, OPENAI_MAX_RETRIES, repr(e))
            if attempt < OPENAI_MAX_RETRIES:
                time.sleep(OPENAI_BACKOFF_BASE ** (attempt - 1))
                continue
        except Exception:
            logger.exception("OpenAI parse failed, пробуем raw fallback")
            try:
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role":"user","content":system_msg+"\n"+user_msg}],
                )
                c = resp.choices[0].message.content
                s, e2 = c.find("{"), c.rfind("}")
                parsed = json.loads(c[s:e2+1]) if s!=-1 and e2!=-1 else json.loads(c)
                parsed = {
                    "region": _cell_str(parsed.get("region","")),
                    "city":   _cell_str(parsed.get("city","")),
                    "street": _cell_str(parsed.get("street","")),
                    "house":  _cell_str(parsed.get("house","")),
                    "etc":    _cell_str(parsed.get("etc","")),
                }
                logger.info(
                    "Parsed (RAW, fallback): region=%s | city=%s | street=%s | house=%s | etc=%s",
                    parsed.get("region",""),parsed.get("city",""),parsed.get("street",""),
                    parsed.get("house",""),parsed.get("etc","")
                )
                return parsed
            except Exception as e2:
                last_err = e2
                logger.exception("Raw fallback тоже не удался")
                break

    logger.error("OpenAI: не удалось распарсить адрес после повторов: %s", repr(last_err))
    return {"region":"", "city":"", "street":"", "house":"", "etc":""}




# ==============================
# Поиск в pharmacies_a
# ==============================
def search_in_pharmacies(conn, city: str, street_clean: str, house_clean: str, inn_clean: str, sales_id=None):
    inn = (inn_clean or "").strip()
    if not inn:
        raise ValueError("ИНН обязателен для поиска (inn_clean пуст)")

    city = (city or "").strip()
    street = (street_clean or "").strip()
    house_norm = (house_clean or "").strip()
    
    # нормализация е/ё для поиска
    city_norm = normalize_yo_text(city)
    street_norm = normalize_yo_text(street)

    house_clause = """
      ( :house_norm IS NULL OR :house_norm = ''
        OR REGEXP_LIKE( UPPER(build), '(^|[^0-9])' || :house_norm || '([^0-9]|$)' )
      )
    """

    with conn.cursor() as cur:
        cur.call_timeout = ORACLE_CALL_TIMEOUT_MS  # <-- таймаут запроса
        # базовые параметры
        params = {"city": city_norm, "street": street_norm, "house_norm": house_norm, "inn": inn}

        # 1) strict: адрес + ИНН через REGEXP_LIKE
        q_strict = f"""
            SELECT id, name, address_d, inns
              FROM pharmacies_a
             WHERE UPPER(source) NOT LIKE 'MDLP%%'
               AND TRANSLATE(UPPER(address_d), 'Ё', 'Е') LIKE '%' || UPPER(:city) || '%'
               AND TRANSLATE(UPPER(address_d), 'Ё', 'Е') LIKE '%' || UPPER(:street) || '%'
               AND {house_clause}
               AND REGEXP_LIKE(inns, '(^|[^0-9])' || :inn || '([^0-9]|$)')
        """
        logger_sql.info("[salesaptek.id=%s] EXEC q_strict\n%s\nPARAMS: %s", sales_id, q_strict.strip(), params)
        for h in logger_sql.handlers:
            if hasattr(h, "flush"):
                try:
                    h.flush()
                except Exception:
                    pass
        try:
            cur.execute(q_strict, params)
            rows = cur.fetchall()
            if rows:
                return rows, "strict_10"   # базовый вариант, достоверность условно 10
        except Exception:
            logger.exception("Ошибка выполнения q_strict для salesaptek.id=%s", sales_id)
            logger_sql.exception("Ошибка выполнения q_strict для salesaptek.id=%s", sales_id)
            raise   # <-- ДОБАВИТЬ

        # 2) fallback: адрес + ИНН через LIKE
        q_fallback = f"""
            SELECT id, name, address_d, inns
              FROM pharmacies_a
             WHERE UPPER(source) NOT LIKE 'MDLP%%'
               AND TRANSLATE(UPPER(address_d), 'Ё', 'Е') LIKE '%' || UPPER(:city) || '%'
               AND TRANSLATE(UPPER(address_d), 'Ё', 'Е') LIKE '%' || UPPER(:street) || '%'
               AND {house_clause}
               AND inns LIKE '%' || :inn || '%'
        """
        logger_sql.info("[salesaptek.id=%s] EXEC q_fallback\n%s\nPARAMS: %s", sales_id, q_fallback.strip(), params)
        for h in logger_sql.handlers:
            if hasattr(h, "flush"):
                try:
                    h.flush()
                except Exception:
                    pass
        try:
            cur.execute(q_fallback, params)
            rows2 = cur.fetchall()
            if rows2:
                return rows2, "fallback_10"
        except Exception:
            logger.exception("Ошибка выполнения q_fallback для salesaptek.id=%s", sales_id)
            logger_sql.exception("Ошибка выполнения q_fallback для salesaptek.id=%s", sales_id)
            raise   # <-- ДОБАВИТЬ

        # 3) strict по build
        q_street_in_build_strict = f"""
            SELECT id, name, address_d, inns
              FROM pharmacies_a
             WHERE UPPER(source) NOT LIKE 'MDLP%%'
               AND UPPER(address_d) LIKE '%' || UPPER(:city) || '%'
               AND TRANSLATE(UPPER(address_d), 'Ё', 'Е') LIKE '%' || UPPER(:street) || '%'
               AND {house_clause}
               AND REGEXP_LIKE(inns, '(^|[^0-9])' || :inn || '([^0-9]|$)')
        """
        logger_sql.info("[salesaptek.id=%s] EXEC q_street_in_build_strict\n%s\nPARAMS: %s",
                        sales_id, q_street_in_build_strict.strip(), params)
        for h in logger_sql.handlers:
            if hasattr(h, "flush"):
                try:
                    h.flush()
                except Exception:
                    pass
        try:
            cur.execute(q_street_in_build_strict, params)
            rows3 = cur.fetchall()
            if rows3:
                return rows3, "street_in_build_strict_10"
        except Exception:
            logger.exception("Ошибка выполнения q_street_in_build_strict для salesaptek.id=%s", sales_id)
            logger_sql.exception("Ошибка выполнения q_street_in_build_strict для salesaptek.id=%s", sales_id)
            raise   # <-- ДОБАВИТЬ

        # 4) fallback по build
        q_street_in_build_like = f"""
            SELECT id, name, address_d, inns
              FROM pharmacies_a
             WHERE UPPER(source) NOT LIKE 'MDLP%%'
               AND TRANSLATE(UPPER(address_d), 'Ё', 'Е') LIKE '%' || UPPER(:city) || '%'
               AND TRANSLATE(UPPER(address_d), 'Ё', 'Е') LIKE '%' || UPPER(:street) || '%'
               AND {house_clause}
               AND inns LIKE '%' || :inn || '%'
        """
        logger_sql.info("[salesaptek.id=%s] EXEC q_street_in_build_like\n%s\nPARAMS: %s",
                        sales_id, q_street_in_build_like.strip(), params)
        for h in logger_sql.handlers:
            if hasattr(h, "flush"):
                try:
                    h.flush()
                except Exception:
                    pass
        try:
            cur.execute(q_street_in_build_like, params)
            rows4 = cur.fetchall()
            if rows4:
                return rows4, "street_in_build_like_10"
        except Exception:
            logger.exception("Ошибка выполнения q_street_in_build_like для salesaptek.id=%s", sales_id)
            logger_sql.exception("Ошибка выполнения q_street_in_build_like для salesaptek.id=%s", sales_id)
            raise   # <-- ДОБАВИТЬ

        # ===================================================
        # Если НИЧЕГО не нашли — дополнительные эвристики
        # с указанием "достоверности"
        # ===================================================

        # разбиение улицы на слова
        words = [w for w in re.split(r"\s+", street) if w]
        if not words:
            return [], "not_found"

        long_words = [w for w in words if len(w) > 2]
        short_words = [w for w in words if len(w) <= 2]

        # Хелпер для выполнения одного "мягкого" запроса по адресу
        def run_soft_query(street_variant: str, label: str, confidence: int):
            local_params = dict(params)
            local_params["street"] = street_variant
            q_soft = f"""
                SELECT id, name, address_d, inns
                  FROM pharmacies_a
                 WHERE UPPER(source) NOT LIKE 'MDLP%%'
                   AND TRANSLATE(UPPER(address_d), 'Ё', 'Е') LIKE '%' || UPPER(:city) || '%'
                   AND TRANSLATE(UPPER(address_d), 'Ё', 'Е') LIKE '%' || UPPER(:street) || '%'
                   AND {house_clause}
                   AND inns LIKE '%' || :inn || '%'
            """
            mode = f"{label}_{confidence}"
            logger_sql.info("[salesaptek.id=%s] EXEC %s\n%s\nPARAMS: %s",
                            sales_id, mode, q_soft.strip(), local_params)
            for h in logger_sql.handlers:
                if hasattr(h, "flush"):
                    try:
                        h.flush()
                    except Exception:
                        pass
            try:
                cur.execute(q_soft, local_params)
                rr = cur.fetchall()
                if rr:
                    logger.info("Эвристика %s дала %s совпадений (достоверность=%s)",
                                label, len(rr), confidence)
                    return rr, mode
            except Exception:
                logger.exception("Ошибка выполнения эвристики %s для salesaptek.id=%s", label, sales_id)
                logger_sql.exception("Ошибка выполнения эвристики %s для salesaptek.id=%s", label, sales_id)
                raise   # <-- ДОБАВИТЬ
            return None, None

        # 1) Если в улице два или более слов:
        #    поменять местами первые два: 1%2 > 2%1, достоверность=9
        if len(words) >= 2:
            w1, w2 = words[0], words[1]
            rest = words[2:]
            first_part = f"{w2}%{w1}"
            street_swapped = " ".join([first_part] + rest) if rest else first_part
            rows_h, mode_h = run_soft_query(street_swapped, "heur_swap", 9)
            if rows_h:
                return rows_h, mode_h

        # 2) Если слова ровно два:
        #    первое слово сокращаем до первой буквы: 1буква%2, достоверность=8
        if len(words) == 2:
            w1, w2 = words[0], words[1]
            if w1:
                first_part = f"{w1[0]}%{w2}"
                rows_h, mode_h = run_soft_query(first_part, "heur_initial", 8)
                if rows_h:
                    return rows_h, mode_h

        # 3) Если есть слова длиной 1–2 буквы и есть длинные (>2),
        #    пробуем искать только по длинным, достоверность=7
        if long_words and short_words:
            street_long = " ".join(long_words)
            rows_h, mode_h = run_soft_query(street_long, "heur_no_short", 7)
            if rows_h:
                return rows_h, mode_h

        # 4) Если есть слова длиной >2, искать по каждому отдельно,
        #    потом объединить, достоверность=5
        if long_words:
            all_rows = []
            for w in long_words:
                rows_h, mode_h = run_soft_query(w, "heur_each_long", 5)
                if rows_h:
                    all_rows.extend(rows_h)
            if all_rows:
                # дедуп по (id,name,address_d,inns)
                seen = set()
                uniq = []
                for r in all_rows:
                    key = (r[0], r[1], r[2], r[3])
                    if key not in seen:
                        seen.add(key)
                        uniq.append(r)
                logger.info("Эвристика heur_each_long дала %s уник. совпадений (достоверность=5)", len(uniq))
                return uniq, "heur_each_long_5"

        # Ничего не нашли
        return [], "not_found"


# =========================
# Экспорт строк (строго из колонки A)
# =========================
def export_main(ws_main, sales_item, pharm_rows, confidence=None):
    """
    Для found == 1:
      - первая строка: salesaptek
      - вторая строка: единственное совпадение из pharmacies_a
      - 5-я колонка: достоверность (confidence)
    """
    conf_str = "" if confidence is None else _cell_str(confidence)

    # строка из salesaptek
    append_from_col_A(ws_main, [[
        _cell_str(sales_item["id"]),
        _cell_str(sales_item["name"]),
        _cell_str(sales_item["or_address"]),
        _cell_str(sales_item["or_inns"]),
        conf_str,
    ]])

    # единственное совпадение из pharmacies_a
    if pharm_rows:
        out = [[
            _cell_str(pid),
            _cell_str(pname),
            _cell_str(paddr),
            _cell_str(pinns),
            conf_str,      # ту же достоверность пишем и для найденной аптеки
        ] for (pid, pname, paddr, pinns) in pharm_rows]
        append_from_col_A(ws_main, out)


def export_unmatched(ws_unmatch, sales_item, parsed):
    append_from_col_A(ws_unmatch, [[
        _cell_str(sales_item["id"]),                # salesaptek.id
        _cell_str(sales_item["name"]),              # name
        _cell_str(sales_item["or_address"]),        # address
        _cell_str(sales_item["or_inns"]),           # inn
        _cell_str(parsed.get("region","")),
        _cell_str(parsed.get("city","")),
        _cell_str(normalize_street_name(parsed.get("street",""))),
        _cell_str(normalize_house_number(parsed.get("house",""))),
        _cell_str(parsed.get("etc","")),            # {} > "" ; непустые dict/list > JSON
    ]])

def export_ambiguous(ws_ambig, sales_item, pharm_rows):
    """
    Для found > 1:
      - первая строка: salesaptek
      - затем несколько строк pharmacies_a
    """
    # исходник salesaptek
    append_from_col_A(ws_ambig, [[
        _cell_str(sales_item["id"]),
        _cell_str(sales_item["name"]),
        _cell_str(sales_item["or_address"]),
        _cell_str(sales_item["or_inns"]),
    ]])
    # все найденные совпадения
    if pharm_rows:
        out = [[_cell_str(pid), _cell_str(pname), _cell_str(paddr), _cell_str(pinns)]
               for (pid, pname, paddr, pinns) in pharm_rows]
        append_from_col_A(ws_ambig, out)

def main():
    t_start = time.perf_counter()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("=== Старт обработки: %s ===", started_at)

    # ================================
    # Google Sheets
    # ================================
    try:
        ws_main, ws_unmatch, ws_ambig = get_worksheets_pair()
    except Exception:
        logger.exception("Ошибка инициализации Google Sheets")
        raise

    # ================================
    # Oracle: подключение с таймаутом
    # ================================
    try:
        conn = connect_oracle_with_timeout()
        conn.call_timeout = ORACLE_CALL_TIMEOUT_MS
        logger.info(
            "Успешное подключение к Oracle (connect_timeout=%s сек, call_timeout=%s мс)",
            ORACLE_CONNECT_TIMEOUT_SEC,
            ORACLE_CALL_TIMEOUT_MS,
        )
    except concurrent.futures.TimeoutError:
        logger.error(
            "Подключение к Oracle не удалось за %s секунд — завершаем работу",
            ORACLE_CONNECT_TIMEOUT_SEC,
        )
        return
    except Exception:
        logger.exception("Не удалось подключиться к Oracle")
        raise

    try:
        # ===============================
        # Загрузка salesaptek с ретраем
        # ===============================
        for attempt in range(1, 3):
            try:
                sales_items = fetch_salesaptek_batch(conn, BATCH_LIMIT)
                break
            except oracledb.DatabaseError as e:
                logger.exception(
                    "Ошибка Oracle при fetch_salesaptek_batch (попытка %s/2): %s",
                    attempt, e,
                )
                if attempt == 2:
                    raise
                try:
                    conn.close()
                except Exception:
                    pass
                logger.info("Пробуем переподключиться к Oracle (после ошибки на fetch)...")
                conn = connect_oracle_with_timeout()
                conn.call_timeout = ORACLE_CALL_TIMEOUT_MS
        else:
            logger.error("Не удалось загрузить sales_items после повторных попыток")
            return

        if not sales_items:
            print("Нет записей для обработки.")
            logger.info("Нет записей для обработки.")
            return

        total_groups = 0
        total_pharm = 0

        # ===============================
        # ОБРАБОТКА КАЖДОГО ЗАПИСИ
        # ===============================
        for idx, item in enumerate(sales_items, start=1):
            total = len(sales_items)
            logger.info(
                "[%02d/%d] salesaptek: id=%s | name=%s | address=%s | inns=%s",
                idx, total,
                _cell_str(item["id"]),
                _cell_str(item["name"]),
                _cell_str(item["or_address"]),
                _cell_str(item["or_inns"])
            )
            print(f"\n[{idx:02d}/{total}] salesaptek: id={item['id']} | name={item['name']}")
            print(f"   address={item['or_address']}")
            print(f"   inns={item['or_inns']}")

            # Парсинг адреса
            try:
                parsed = gpt_parse_address(item["or_address"])
            except Exception as e:
                logger.exception("Ошибка парсинга адреса для salesaptek.id=%s", item["id"])
                print(f"  ?? Ошибка парсинга адреса: {e}")
                export_unmatched(ws_unmatch, item, {"region":"","city":"","street":"","house":"","etc":""})
                continue

            region = parsed.get("region","")
            city   = parsed.get("city","")
            street_raw = parsed.get("street","")
            house_raw  = parsed.get("house","")
            etc    = parsed.get("etc","")

            street = normalize_street_name(street_raw)
            house  = normalize_house_number(house_raw)

            logger.info(
                "Parsed > region=%s | city=%s | street_raw=%s | street=%s | house_raw=%s | house=%s | etc=%s",
                region, city, street_raw, street, house_raw, house, etc
            )

            # Нет адресных данных — сразу в непривязавшиеся
            if (not city.strip()) and (not street.strip()) and (not house.strip()):
                logger.info("Пропуск поиска: пустые city/street/house (salesaptek.id=%s)", item["id"])
                export_unmatched(ws_unmatch, item, {
                    "region": region, "city": city, "street": street, "house": house, "etc": etc
                })
                print("  ? Пустые city/street/house — добавлено в непривязавшиеся.")
                continue

            # ИНН токены
            inns_tokens = re.findall(r"\d{6,}", item["or_inns"] or "")
            inns_tokens = [t.strip() for t in inns_tokens if t.strip()]
            if not inns_tokens:
                logger.info("Пропуск: нет валидных ИНН (salesaptek.id=%s)", item["id"])
                export_unmatched(ws_unmatch, item, {
                    "region": region, "city": city, "street": street, "house": house, "etc": etc
                })
                continue
            logger.info("ИНН-токены: %s", inns_tokens)

            # Поиск с ретраем
            rows_all, modes = [], set()
            for inn in inns_tokens:
                for attempt in range(1, 3):
                    try:
                        rows, mode = search_in_pharmacies(
                            conn, city, street, house, inn, sales_id=item["id"]
                        )
                        if rows:
                            rows_all.extend(rows)
                        if mode:
                            modes.add(mode)
                        break
                    except oracledb.DatabaseError as e:
                        logger.exception(
                            "Ошибка Oracle при поиске (salesaptek.id=%s, inn=%s, попытка %s/2): %s",
                            item["id"], inn, attempt, e
                        )
                        if attempt == 2:
                            break
                        try:
                            conn.close()
                        except Exception:
                            pass
                        logger.info("Пробуем переподключиться к Oracle...")
                        conn = connect_oracle_with_timeout()
                        conn.call_timeout = ORACLE_CALL_TIMEOUT_MS
                    except Exception:
                        logger.exception(
                            "Ошибка поиска в pharmacies_a для salesaptek.id=%s (inn=%s)",
                            item["id"], inn
                        )
                        break

            # Дедуп
            seen, unique_rows = set(), []
            for r in rows_all:
                key = (r[0], r[1], r[2], r[3])
                if key not in seen:
                    seen.add(key)
                    unique_rows.append(r)

            found = len(unique_rows)
            logger.info("Найдено совпадений: %s | режим(ы): %s", found, ", ".join(sorted(modes)) if modes else "—")
            print(f"  Найдено в pharmacies_a: {found}")

            confidence = _extract_confidence_from_modes(modes)

            if found == 0:
                export_unmatched(ws_unmatch, item, {
                    "region": region, "city": city, "street": street, "house": house, "etc": etc
                })
                print(f"{RED}  > Нет совпадений > лист непривязавшиеся{RESET}")
                continue

            if found == 1:
                export_main(ws_main, item, unique_rows, confidence)
                total_groups += 1
                total_pharm += found
                print(f"{GREEN}  > 1 совпадение > основной лист (достоверность={confidence}){RESET}")
                continue

            export_ambiguous(ws_ambig, item, unique_rows)
            total_groups += 1
            total_pharm += found
            print(f"  > {found} совпадений > лист неоднозначности")

        logger.info("Итог: групп с совпадениями=%s, строк pharmacies_a=%s", total_groups, total_pharm)
        print(f"\n? Готово. Групп с совпадениями: {total_groups}, строк pharmacies_a: {total_pharm}")

    finally:
        try:
            conn.close()
        except Exception:
            pass
        finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elapsed = time.perf_counter() - t_start
        elapsed_str = _format_elapsed(elapsed)
        logger.info(
            "=== Завершено === | Старт: %s | Финиш: %s | Длительность: %s",
            started_at, finished_at, elapsed_str
        )
        print(f"? Общее время выполнения: {elapsed_str} (начало: {started_at}, конец: {finished_at})")
if __name__ == "__main__":
    print("> Запуск main() из as_3.py")
    main()
else:
    logger.info("as_3.py импортирован как модуль (__name__=%s), main() не запускаем", __name__)


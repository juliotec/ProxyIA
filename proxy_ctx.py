import hashlib
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = "http://127.0.0.1:1235"
MAX_PROMPT_TOKENS = 61000   # Dispara limpieza antes de tocar los 65,536 de la 9070 XT
KEEP_TARGET_TOKENS = 38000  # Conserva tus instrucciones completas + historial reciente con amplio margen
MAX_GEN_TOKENS = 16384      # Evita bucles gigantes de escritura que llenen los 65,536 tokens
FAST_SKIP_CHARS = 155000    # Debajo de 155k chars (~43k tok reales) evita llamadas HTTP previas de tokenizado
PROGRESS_PATH = r"C:\Julio\Agente\progreso.md"

# Whitelist de herramientas permitidas (el resto de schemas se filtran para ahorrar VRAM):
#
# Visual Studio 2026 (27 schemas totales):
#   Terminal:    powershell (3189c), read_powershell (1077c), stop_powershell (511c), list_powershell (410c)
#   Archivos:    replace_string_in_file (3724c), multi_replace_string_in_file (1761c), get_file (1044c), view (860c), create_file (530c), remove_file (326c)
#   Busqueda:    grep (1558c), glob (519c), semantic_code_search (498c)
#   Simbolos:    get_symbol_class_hierarchy (3215c), get_symbol_call_hierarchy (2806c)
#   Subagentes:  task (4411c), write_agent (2160c), read_agent (1330c), list_agents (1294c), task_complete (606c)
#   Build/Tests: get_tests (868c), run_tests (676c), run_build (587c)
#   IDE/Web:     ask_question (1388c), tool_search_tool (951c), skill (833c), web_fetch (734c)
#
# OpenCode v2.4+ (17 schemas totales):
#   Terminal:    shell (1604c), bash (alias v1)
#   CodeMode:    execute (1378c)
#   Archivos:    read, write, edit, patch
#   Busqueda:    grep, glob, list
#   Web:         webfetch, websearch
#   Tareas:      todowrite, todoread
#   Subagentes:  task
#   IDE/Skills:  skill (507c), question
ALLOWED_TOOL_NAMES = {"powershell", "shell", "skill"}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "proxy.log")
LOG_LOCK = threading.Lock()

RE_WS = re.compile(r"\s+")
RE_MD = re.compile(r"[\w\-.]+\.md\b")
RE_CODE = re.compile(r"[\w\-.]+\.(?:cpp|hpp|c|h|py|cs|yml|yaml|json|glsl|spv|bat|ps1)\b")

SYS_CACHE = {"hash": "", "has_copilot": False, "md": [], "code": []}
HTTP_OPENER = urllib.request.build_opener()

STATE_LOCK = threading.Lock()
RE_READ_CMD = re.compile(r"(?:get-content|select-string|readalllines|readalltext|\.read\(|open\()", re.I)

STATE = {
    "order_num": 0,
    "last_user_hash": "",
    "step_num": 0,
    "recent_sigs": [],
    "repeat_count": 0,
    "ide_compacted": False,
    "consec_reads": 0,
    "fast_mode": False,
}


os.system("")  # Activa soporte ANSI nativo en consola Windows 11
C_RST = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[90m"
C_CYAN = "\033[96m"
C_BLUE = "\033[94m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_MAGENTA = "\033[95m"
C_WHITE = "\033[97m"
C_RED = "\033[91m"


def colorize_line(line: str) -> str:
    s = line
    s = re.sub(r"(\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\])", f"{C_DIM}\\1{C_RST}", s, count=1)
    s = re.sub(r"(\[ORDEN #\d+(?: \| [^\]]+)?\])", f"{C_BOLD}{C_BLUE}\\1{C_RST}", s, count=1)
    s = s.replace("[PROXY]", f"{C_BOLD}{C_CYAN}[PROXY]{C_RST}").replace("[PROXY-TOOLS]", f"{C_BOLD}{C_CYAN}[PROXY-TOOLS]{C_RST}")
    s = s.replace("[PROXY-PRUNE]", f"{C_BOLD}{C_YELLOW}[PROXY-PRUNE]{C_RST}")
    s = s.replace("[SYS-CHECK:", f"{C_DIM}[SYS-CHECK:")
    if "[SYS-CHECK:" in line:
        s += C_RST
    s = s.replace("[CTX:", f"{C_CYAN}[CTX:{C_RST}")
    s = s.replace("[RUN]", f"{C_BOLD}{C_GREEN}[RUN]{C_RST}")
    s = s.replace("[TOOLS-CALL]", f"{C_BOLD}{C_YELLOW}[TOOLS-CALL]{C_RST}")
    s = re.sub(r"(\[THINK-FULL \([^)]+\)\] >>>|<<< \[END-THINK\])", f"{C_MAGENTA}\\1{C_RST}", s)
    s = re.sub(r"(\[OUT-FULL \([^)]+\)\] >>>|<<< \[END-OUT\])", f"{C_BOLD}{C_WHITE}\\1{C_RST}", s)
    s = re.sub(r"(\[ALERTA[^\]]*\])", f"{C_BOLD}{C_RED}\\1{C_RST}", s)
    return s


def log_line(msg: str) -> None:
    print(colorize_line(msg), flush=True)
    try:
        with LOG_LOCK:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
    except Exception:
        pass


def log_batch(lines: list) -> None:
    if not lines:
        return
    colored_block = "\n".join(colorize_line(l) for l in lines)
    plain_block = "\n".join(lines)
    print(colored_block, flush=True)
    try:
        with LOG_LOCK:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(plain_block + "\n")
    except Exception:
        pass


def compact_snippet(text: str, max_len: int = 180) -> str:
    if not text:
        return ""
    clean = RE_WS.sub(" ", str(text)).strip()
    if len(clean) <= max_len:
        return clean
    half = max_len // 2
    return clean[:half] + " ... " + clean[-half:]


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content) if content is not None else ""


def filter_tools_schema(tools) -> tuple:
    """Filtra esquemas prohibidos y devuelve (tools_filtradas, total_original, chars_ahorrados)."""
    if not isinstance(tools, list) or not tools:
        return tools, 0, 0
    orig_count = len(tools)
    if not getattr(filter_tools_schema, "_logged", False):
        filter_tools_schema._logged = True
        items = sorted([(len(json.dumps(t, separators=(",", ":"))), ((t.get("function") or {}).get("name") or t.get("name", "?"))) for t in tools if isinstance(t, dict)], reverse=True)
        log_line("[PROXY-TOOLS] " + ", ".join(f"{n}({c}c)" for c, n in items))
    orig_chars = len(json.dumps(tools, separators=(",", ":")))
    kept = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name", "")
        if name in ALLOWED_TOOL_NAMES:
            kept.append(t)
    if not kept:
        return tools, orig_count, 0
    new_chars = len(json.dumps(kept, separators=(",", ":")))
    return kept, orig_count, max(orig_chars - new_chars, 0)


def get_tools_chars(tools) -> int:
    if not isinstance(tools, list) or not tools:
        return 0
    return len(json.dumps(tools, separators=(",", ":")))


def post_json(endpoint: str, payload: dict) -> dict:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{TARGET}{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with HTTP_OPENER.open(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def count_tokens(messages: list, tools: list = None) -> int:
    try:
        tpl_payload = {"messages": messages}
        if tools:
            tpl_payload["tools"] = tools
        prompt = post_json("/apply-template", tpl_payload).get("prompt", "")
        tokens = post_json("/tokenize", {"content": prompt}).get("tokens", [])
        return len(tokens)
    except Exception:
        return sum(len(str(m)) for m in messages) // 3


def trim_msg_content(msg: dict, max_chars: int = 12000) -> bool:
    content = msg.get("content")
    half = max_chars // 2
    if isinstance(content, str) and len(content) > max_chars:
        msg["content"] = content[:half] + "\n...[RECORTADO POR LIMITE DE VRAM]...\n" + content[-half:]
        return True
    if isinstance(content, list):
        changed = False
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and len(part["text"]) > max_chars:
                t = part["text"]
                part["text"] = t[:half] + "\n...[RECORTADO POR LIMITE DE VRAM]...\n" + t[-half:]
                changed = True
        return changed
    return False


def is_ide_compaction_prompt(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    txt = extract_text(msg.get("content")).lstrip()
    return txt.startswith("Please create a detailed summary of the conversation")


def is_real_user_msg(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    txt = extract_text(msg.get("content")).lstrip()
    if not txt:
        return False
    if txt.startswith("<system_reminder>") or txt.startswith("<environment_info>"):
        return False
    if is_ide_compaction_prompt(msg):
        return False
    return True


def inject_hint_into_last_user(messages: list, hint: str) -> None:
    if not hint or not messages:
        return
    for i in range(len(messages) - 1, -1, -1):
        if is_real_user_msg(messages[i]):
            m = dict(messages[i])
            content = m.get("content")
            if isinstance(content, str):
                if hint not in content:
                    m["content"] = content + "\n\n" + hint
                    messages[i] = m
            elif isinstance(content, list):
                parts = [dict(part) if isinstance(part, dict) else part for part in content]
                attached = False
                for part in reversed(parts):
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        if hint not in part["text"]:
                            part["text"] = part["text"] + "\n\n" + hint
                        attached = True
                        break
                if not attached:
                    parts.append({"type": "text", "text": hint})
                m["content"] = parts
                messages[i] = m
            break


def read_active_progress(max_chars: int = 4000) -> str:
    try:
        if os.path.isfile(PROGRESS_PATH):
            with open(PROGRESS_PATH, "r", encoding="utf-8", errors="ignore") as f:
                data = f.read().strip()
            if data:
                return data[:max_chars]
    except Exception:
        pass
    return ""


# Comando rapido al inicio del prompt:
#   !fast -> Apaga <think> (0 tokens de razonamiento) y aplica temp=0.15, top_p=0.8, presence_penalty=0.0, max_tokens=4096
RE_FAST_CMD = re.compile(r"(?:^|\n|>)\s*!fast(?:[\s:\-\t\r\n]+|$)", re.I)


def apply_thinking_switch(messages: list) -> bool:
    """Detecta !fast en el ultimo mensaje real del usuario y mantiene el estado en toda la orden."""
    if not messages:
        return True

    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if is_real_user_msg(messages[i]):
            last_user_idx = i
            break

    if last_user_idx == -1:
        with STATE_LOCK:
            STATE["fast_mode"] = False
        return True

    target = dict(messages[last_user_idx])
    txt = extract_text(target.get("content"))
    m_fast = RE_FAST_CMD.search(txt)

    with STATE_LOCK:
        STATE["fast_mode"] = bool(m_fast)

    if not m_fast:
        return True

    if isinstance(target.get("content"), str):
        target["content"] = RE_FAST_CMD.sub(" ", target["content"], count=1).strip()
    elif isinstance(target.get("content"), list):
        parts = [dict(part) if isinstance(part, dict) else part for part in target["content"]]
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and RE_FAST_CMD.search(part["text"]):
                part["text"] = RE_FAST_CMD.sub(" ", part["text"], count=1).strip()
                break
        target["content"] = parts

    messages[last_user_idx] = target
    return False


def inspect_incoming(messages: list, tools: list = None, req_params: dict = None, tools_filter_info: tuple = (0, 0)) -> dict:
    sys_texts = []
    non_sys_texts = []
    non_sys = []
    role_counts = {"sys": 0, "user": 0, "asst": 0, "tool": 0}
    role_chars = {"sys": 0, "user": 0, "asst": 0, "tool": 0}
    hist_tools = {}

    for m in messages:
        r = m.get("role", "")
        txt = extract_text(m.get("content"))
        c_len = len(txt)
        if r in ("system", "developer"):
            role_counts["sys"] += 1
            role_chars["sys"] += c_len
            sys_texts.append(txt)
        else:
            non_sys.append((m, txt))
            non_sys_texts.append(txt)
            if r == "user":
                role_counts["user"] += 1
                role_chars["user"] += c_len
            elif r == "assistant":
                role_counts["asst"] += 1
                r_txt = m.get("reasoning_content") or m.get("reasoning") or ""
                tc_chars = 0
                for tc in m.get("tool_calls") or []:
                    fn_obj = tc.get("function") or {}
                    fn_name = fn_obj.get("name") or "tool"
                    hist_tools[fn_name] = hist_tools.get(fn_name, 0) + 1
                    tc_chars += len(fn_name) + len(str(fn_obj.get("arguments") or ""))
                role_chars["asst"] += c_len + len(str(r_txt)) + tc_chars
            elif r == "tool":
                role_counts["tool"] += 1
                role_chars["tool"] += c_len

    sys_joined = "\n".join(sys_texts)
    sys_digest = hashlib.sha1(sys_joined.encode("utf-8", errors="ignore")).hexdigest()
    sys_snap = SYS_CACHE
    if sys_digest != sys_snap["hash"]:
        sys_snap = {
            "hash": sys_digest,
            "has_copilot": ("Copilot Instructions" in sys_joined) or ("UPSTREAM.md" in sys_joined),
            "md": sorted(set(RE_MD.findall(sys_joined))),
            "code": sorted(set(RE_CODE.findall(sys_joined))),
        }
        globals()["SYS_CACHE"] = sys_snap

    non_sys_joined = "\n".join(non_sys_texts)
    has_copilot = sys_snap["has_copilot"] or ("Copilot Instructions" in non_sys_joined) or ("UPSTREAM.md" in non_sys_joined)
    md_files = sorted(set(sys_snap["md"]) | set(RE_MD.findall(non_sys_joined)))
    code_files = sorted(set(sys_snap["code"]) | set(RE_CODE.findall(non_sys_joined)))

    last_msg, last_txt = non_sys[-1] if non_sys else ({}, "")
    last_role = last_msg.get("role", "")

    real_user_txt = ""
    for m_u, t_u in reversed(non_sys):
        if is_real_user_msg(m_u):
            real_user_txt = t_u
            break
    user_digest = hashlib.sha1(real_user_txt.encode("utf-8", errors="ignore")).hexdigest() if real_user_txt else ""

    runtime_hints = []
    with STATE_LOCK:
        if STATE.pop("force_break_loop", False):
            runtime_hints.append("[AVISO CRITICO DEL PROXY: Acabas de repetir una llamada o ciclo de lectura/herramienta (incluso cambiando numeros de archivo o rangos). PROHIBIDO repetirlo: usa la informacion que ya tienes en pantalla y responde o avanza al siguiente paso ahora.]")
        if is_ide_compaction_prompt(last_msg):
            STATE["ide_compacted"] = True
            STATE["consec_reads"] = 0
            label = f"[ORDEN #{STATE['order_num']} | COMPACT-IDE]"
            trigger_info = f'IDECompact({len(last_txt)}c)'
        elif user_digest and user_digest != STATE.get("last_user_hash", "") and last_role != "tool":
            STATE["last_user_hash"] = user_digest
            STATE["order_num"] += 1
            STATE["step_num"] = 0
            STATE["recent_sigs"] = []
            STATE["repeat_count"] = 0
            STATE["consec_reads"] = 0
            if STATE["ide_compacted"]:
                STATE["ide_compacted"] = False
                prog_now = read_active_progress()
                if prog_now:
                    runtime_hints.append("[AVISO PROXY POST-COMPACTACION: NO vuelvas a leer archivos completos ni empieces desde cero. Continua desde el primer [ ] pendiente de C:\\Julio\\Agente\\progreso.md:]\n" + prog_now)
                else:
                    runtime_hints.append("[AVISO PROXY POST-COMPACTACION: Visual Studio compacto el historial. NO vuelvas a leer archivos largos. Crea C:\\Julio\\Agente\\progreso.md YA como primera accion y ejecuta los cambios via script Python en C:\\Julio\\Agente\\Temp sin imprimir archivos largos.]")
            label = f"[ORDEN #{STATE['order_num']}]"
            trigger_info = f'UserReq({len(real_user_txt)}c): "{compact_snippet(real_user_txt, 220)}"'
        else:
            if STATE["order_num"] == 0:
                STATE["order_num"] = 1
            STATE["step_num"] += 1
            if STATE.get("ide_compacted"):
                STATE["ide_compacted"] = False
                prog_now = read_active_progress()
                if prog_now:
                    runtime_hints.append("[AVISO PROXY POST-COMPACTACION: NO vuelvas a leer archivos completos ni empieces desde cero. Continua desde el primer [ ] pendiente de C:\\Julio\\Agente\\progreso.md:]\n" + prog_now)
                else:
                    runtime_hints.append("[AVISO PROXY POST-COMPACTACION: Visual Studio compacto el historial. NO vuelvas a leer archivos largos. Crea C:\\Julio\\Agente\\progreso.md YA como primera accion y ejecuta los cambios via script Python en C:\\Julio\\Agente\\Temp sin imprimir archivos largos.]")
            label = f"[ORDEN #{STATE['order_num']} | PASO #{STATE['step_num']}]"
            t_name = last_msg.get("name") or last_role or "step"
            low = last_txt.lower()
            is_err = (
                ("completed with exit code " in low and "completed with exit code 0" not in low)
                or "traceback (most recent call last):" in low
                or "unicodeencodeerror:" in low
            )
            status_tag = "ERR" if is_err else "OK"
            trigger_info = f'PrevTool({t_name}={status_tag}, {len(last_txt)}c): "{compact_snippet(last_txt, 200)}"'
            is_read = t_name in ("get_file", "view", "view_range", "grep") or (t_name == "powershell" and len(last_txt) > 3500 and RE_READ_CMD.search(last_txt))
            STATE["consec_reads"] = (STATE["consec_reads"] + 1) if is_read else 0
            if STATE["consec_reads"] >= 3:
                if STATE.get("fast_mode", False):
                    STATE["fast_mode"] = False
                if not os.path.isfile(PROGRESS_PATH):
                    runtime_hints.append("[AVISO ANTI-LOOP PROXY: Llevas 3+ lecturas seguidas sin crear C:\\Julio\\Agente\\progreso.md. DETEN lecturas, crea C:\\Julio\\Agente\\progreso.md YA y edita dentro de un script Python en Temp sin imprimir archivos largos.]")
                else:
                    runtime_hints.append("[AVISO ANTI-LOOP PROXY: Llevas 3+ lecturas seguidas. No imprimas mas texto largo; ejecuta ya el siguiente [ ] de C:\\Julio\\Agente\\progreso.md con un script Python en Temp.]")

    injected_chars = 0
    if runtime_hints:
        joined_hints = "\n\n".join(runtime_hints)
        inject_hint_into_last_user(messages, joined_hints)
        injected_chars = len(joined_hints) + 2

    tools_chars = get_tools_chars(tools)
    orig_schemas, saved_schema_chars = tools_filter_info

    return {
        "label": label,
        "has_copilot": has_copilot,
        "sys_chars": role_chars["sys"],
        "all_chars": role_chars["sys"] + role_chars["user"] + role_chars["asst"] + role_chars["tool"] + tools_chars + injected_chars,
        "md_files": md_files,
        "code_files": code_files,
        "roles": role_counts,
        "role_chars": role_chars,
        "hist_tools": hist_tools,
        "tools_schema_count": len(tools) if isinstance(tools, list) else 0,
        "orig_schema_count": orig_schemas,
        "saved_schema_chars": saved_schema_chars,
        "req_params": req_params or {},
        "trigger_info": trigger_info,
    }


def prune_if_needed(messages: list, tools: list = None, approx_chars: int = None):
    if not messages:
        return messages, 0, None

    if approx_chars is None:
        approx_chars = sum(
            len(extract_text(m.get("content")))
            + len(str(m.get("reasoning_content") or m.get("reasoning") or ""))
            + sum(
                len(str(((tc.get("function") or {}).get("name") or "")))
                + len(str(((tc.get("function") or {}).get("arguments") or "")))
                for tc in (m.get("tool_calls") or [])
            )
            for m in messages
        ) + get_tools_chars(tools)

    if approx_chars < FAST_SKIP_CHARS:
        est_tok = max(int(approx_chars / 3.6), 1)
        return messages, est_tok, None

    total = count_tokens(messages, tools)
    if total <= MAX_PROMPT_TOKENS:
        return messages, total, None

    sys_msgs = [m for m in messages if m.get("role") in ("system", "developer")]
    rest = [m for m in messages if m.get("role") not in ("system", "developer")]

    dropped_msgs = 0
    dropped_orphan_tools = 0
    extreme_trimmed = 0

    last_user_idx = -1
    for i in range(len(rest) - 1, -1, -1):
        if is_real_user_msg(rest[i]):
            last_user_idx = i
            break
    if last_user_idx == -1:
        for i in range(len(rest) - 1, -1, -1):
            if rest[i].get("role") == "user":
                last_user_idx = i
                break

    if last_user_idx > 0:
        removed_old = rest[:last_user_idx]
        dropped_orphan_tools += sum(1 for m in removed_old if m.get("role") == "tool")
        dropped_msgs += len(removed_old) - dropped_orphan_tools
        rest = rest[last_user_idx:]

    anchor = []
    progress_attached = False
    if rest and rest[0].get("role") == "user":
        anchor_msg = dict(rest.pop(0))
        prog_txt = read_active_progress()
        if prog_txt:
            orig_txt = extract_text(anchor_msg.get("content"))
            if "[ESTADO ACTUAL EN DISCO" not in orig_txt:
                anchor_msg["content"] = (
                    orig_txt
                    + "\n\n[ESTADO ACTUAL EN DISCO C:\\Julio\\Agente\\progreso.md - CONTINUA DESDE EL PRIMER [ ] PENDIENTE]:\n"
                    + prog_txt
                )
                progress_attached = True
        anchor = [anchor_msg]

    while rest and rest[0].get("role") == "tool":
        rest.pop(0)
        dropped_orphan_tools += 1

    if len(rest) > 2:
        valid_cuts = []
        idx = 0
        while idx < len(rest) - 2:
            next_idx = idx + 1
            while next_idx < len(rest) - 2 and rest[next_idx].get("role") == "tool":
                next_idx += 1
            valid_cuts.append(next_idx)
            idx = next_idx

        if valid_cuts:
            target_chars = int(KEEP_TARGET_TOKENS * 3.6)
            def _msg_c(m):
                return len(extract_text(m.get("content"))) + sum(len(str(((tc.get("function") or {}).get("name") or ""))) + len(str(((tc.get("function") or {}).get("arguments") or ""))) for tc in (m.get("tool_calls") or []))
            base_chars = sum(_msg_c(m) for m in (sys_msgs + anchor)) + get_tools_chars(tools)
            rest_lens = [_msg_c(m) for m in rest]
            suffix_sum = sum(rest_lens)
            best_cut = valid_cuts[-1]
            prev_c = 0
            for cut in valid_cuts:
                suffix_sum -= sum(rest_lens[prev_c:cut])
                prev_c = cut
                if base_chars + suffix_sum <= target_chars:
                    best_cut = cut
                    break
            removed = rest[:best_cut]
            orph = sum(1 for m in removed if m.get("role") == "tool")
            dropped_orphan_tools += orph
            dropped_msgs += len(removed) - orph
            rest = rest[best_cut:]

    if count_tokens(sys_msgs + anchor + rest, tools) > MAX_PROMPT_TOKENS:
        protected = max(len(rest) - 2, 0)
        for idx_m, msg in enumerate(rest):
            if idx_m >= protected:
                break
            if trim_msg_content(msg, 10000):
                extreme_trimmed += 1
                if count_tokens(sys_msgs + anchor + rest, tools) <= MAX_PROMPT_TOKENS:
                    break
        if count_tokens(sys_msgs + anchor + rest, tools) > MAX_PROMPT_TOKENS and anchor:
            if trim_msg_content(anchor[0], 10000):
                extreme_trimmed += 1
        if count_tokens(sys_msgs + anchor + rest, tools) > MAX_PROMPT_TOKENS:
            for msg in anchor + rest:
                if trim_msg_content(msg, 3500):
                    extreme_trimmed += 1
                    if count_tokens(sys_msgs + anchor + rest, tools) <= MAX_PROMPT_TOKENS:
                        break
        while len(rest) > 1 and count_tokens(sys_msgs + anchor + rest, tools) > MAX_PROMPT_TOKENS:
            popped = rest.pop(0)
            if popped.get("role") == "tool":
                dropped_orphan_tools += 1
            else:
                dropped_msgs += 1
            while rest and rest[0].get("role") == "tool":
                rest.pop(0)
                dropped_orphan_tools += 1

    result = sys_msgs + anchor + rest
    final_tokens = count_tokens(result, tools)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prog_tag = "SI" if progress_attached else "NO"
    prune_msg = (
        f"[{now_str}] [PROXY-PRUNE] Recortado de {total} -> {final_tokens} tok | "
        f"Eliminados: {dropped_msgs} msgs + {dropped_orphan_tools} orphan_tools | "
        f"ExtremeTrim: {extreme_trimmed} | ProgresoDiscoAnclado: {prog_tag}"
    )
    log_line(prune_msg)
    return result, final_tokens, (total, final_tokens)


def check_loop(sig_raw: str, prev_err: bool = False) -> str:
    if not sig_raw:
        return ""
    norm = re.sub(r"\d+", "#", sig_raw)
    norm = re.sub(r'\becho\s+(?:"[^"]*"|\x27[^\x27]*\x27|\S+)', 'echo "', norm, flags=re.I)
    digest = hashlib.sha1(norm.encode("utf-8", errors="ignore")).hexdigest()
    with STATE_LOCK:
        recent = STATE.setdefault("recent_sigs", [])
        is_repeat = digest in recent
        recent.append(digest)
        if len(recent) > 4:
            del recent[0]
        if is_repeat and not prev_err:
            if STATE["repeat_count"] < 1:
                STATE["repeat_count"] += 1
                return ""
            STATE["repeat_count"] += 1
            escalated = ""
            if STATE.get("fast_mode", False):
                STATE["fast_mode"] = False
                escalated = " [AUTO-DEEP]"
            return f" [ALERTA LOOP x{STATE['repeat_count'] + 1}]{escalated}"
        STATE["repeat_count"] = 0
        return ""


class SilentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _forward(self):
        url = f"{TARGET}{self.path}"
        content_len = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_len) if content_len > 0 else None

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("host", "content-length", "transfer-encoding", "connection")
        }

        is_chat = self.command == "POST" and "/chat/completions" in self.path
        is_stream = False
        meta_in = None
        ctx_tokens_sent = 0

        if raw_body and self.command == "POST":
            try:
                body = json.loads(raw_body.decode("utf-8"))
                tools_filter_info = (0, 0)
                if "tools" in body and isinstance(body["tools"], list):
                    filtered_tools, orig_cnt, saved_c = filter_tools_schema(body["tools"])
                    body["tools"] = filtered_tools
                    tools_filter_info = (orig_cnt, saved_c)
                    tc_req = body.get("tool_choice")
                    if isinstance(tc_req, dict):
                        tc_fn = (tc_req.get("function") or {}).get("name") or tc_req.get("name")
                        if tc_fn and tc_fn not in ALLOWED_TOOL_NAMES:
                            body["tool_choice"] = "auto"

                if is_chat:
                    think_on = apply_thinking_switch(body.get("messages") or [])
                    ctk = body.get("chat_template_kwargs")
                    if not isinstance(ctk, dict):
                        ctk = {}
                    ctk["enable_thinking"] = think_on
                    body["chat_template_kwargs"] = ctk
                    if not think_on:
                        body["temperature"] = 0.15
                        body["top_p"] = 0.8
                        body["presence_penalty"] = 0.0
                        body["max_tokens"] = 4096
                    else:
                        cur_max = body.get("max_tokens")
                        if not isinstance(cur_max, int) or cur_max <= 0 or cur_max > MAX_GEN_TOKENS:
                            body["max_tokens"] = MAX_GEN_TOKENS
                    is_stream = bool(body.get("stream", False))
                    if is_stream:
                        stream_opts = body.get("stream_options")
                        if not isinstance(stream_opts, dict):
                            stream_opts = {}
                        stream_opts["include_usage"] = True
                        body["stream_options"] = stream_opts
                    think_tag = "DEEP" if think_on else "FAST"
                    req_params = {
                        "temp": body.get("temperature", "def"),
                        "top_p": body.get("top_p", "def"),
                        "max_tok": body.get("max_tokens"),
                        "stream": is_stream,
                        "think": think_tag,
                    }
                else:
                    req_params = {}

                if "messages" in body and isinstance(body["messages"], list):
                    for _m in body["messages"]:
                        if isinstance(_m, dict) and _m.get("role") == "assistant":
                            _m.pop("reasoning_content", None)
                            _m.pop("reasoning", None)
                if "messages" in body and isinstance(body["messages"], list):
                    _cleaned_msgs = []
                    _seen_tc = False
                    for _msg_t in body["messages"]:
                        if not isinstance(_msg_t, dict):
                            continue
                        _r_t = _msg_t.get("role")
                        if _r_t == "assistant":
                            _seen_tc = bool(_msg_t.get("tool_calls"))
                            _cleaned_msgs.append(_msg_t)
                        elif _r_t == "tool":
                            if _seen_tc:
                                trim_msg_content(_msg_t, 12000)
                                _cleaned_msgs.append(_msg_t)
                        else:
                            _seen_tc = False
                            _cleaned_msgs.append(_msg_t)
                    body["messages"] = _cleaned_msgs
                    approx_c = None
                    if is_chat:
                        meta_in = inspect_incoming(body["messages"], body.get("tools"), req_params, tools_filter_info)
                        approx_c = meta_in["all_chars"]
                    body["messages"], ctx_tokens_sent, _ = prune_if_needed(
                        body["messages"], body.get("tools"), approx_c
                    )
                raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
            except Exception as _e_body:
                log_line(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PROXY-ERR] Error procesando request: {_e_body}")

        headers["Connection"] = "keep-alive"
        if raw_body is not None:
            headers["Content-Length"] = str(len(raw_body))

        t_start = time.perf_counter()
        t_first_token = None
        stream_chunks = 0
        timings_data = None
        usage_data = None
        finish_reason = "unknown"

        think_parts = []
        out_parts = []
        tool_calls_acc = {}

        req = urllib.request.Request(url, data=raw_body, headers=headers, method=self.command)
        try:
            with HTTP_OPENER.open(req, timeout=None) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()

                while True:
                    chunk = resp.readline()
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()

                    if is_chat:
                        line = chunk.decode("utf-8", errors="ignore").strip()
                        if line.startswith("data: "):
                            payload_str = line[6:]
                            if payload_str != "[DONE]":
                                try:
                                    evt = json.loads(payload_str)
                                    choices = evt.get("choices") or []
                                    if choices:
                                        ch0 = choices[0]
                                        if ch0.get("finish_reason"):
                                            finish_reason = ch0["finish_reason"]
                                        delta = ch0.get("delta") or {}
                                        if delta:
                                            if t_first_token is None:
                                                t_first_token = time.perf_counter()
                                            stream_chunks += 1
                                        r_txt = delta.get("reasoning_content") or delta.get("reasoning")
                                        if isinstance(r_txt, str):
                                            think_parts.append(r_txt)
                                        c_txt = delta.get("content")
                                        if isinstance(c_txt, str):
                                            out_parts.append(c_txt)
                                        for tc in delta.get("tool_calls") or []:
                                            idx = tc.get("index") if tc.get("index") is not None else 0
                                            entry = tool_calls_acc.setdefault(idx, {"name": "", "args": []})
                                            fn = tc.get("function") or {}
                                            if fn.get("name"):
                                                entry["name"] += fn["name"]
                                            if fn.get("arguments"):
                                                entry["args"].append(fn["arguments"])
                                    if evt.get("timings"):
                                        timings_data = evt["timings"]
                                    if evt.get("usage"):
                                        usage_data = evt["usage"]
                                except Exception:
                                    pass
                        elif not is_stream and line.startswith("{"):
                            try:
                                evt = json.loads(line)
                                choices = evt.get("choices") or []
                                if choices:
                                    ch0 = choices[0]
                                    if ch0.get("finish_reason"):
                                        finish_reason = ch0["finish_reason"]
                                    msg_obj = ch0.get("message") or {}
                                    r_txt = msg_obj.get("reasoning_content") or msg_obj.get("reasoning")
                                    if isinstance(r_txt, str):
                                        think_parts.append(r_txt)
                                    c_txt = msg_obj.get("content")
                                    if isinstance(c_txt, str):
                                        out_parts.append(c_txt)
                                    for idx, tc in enumerate(msg_obj.get("tool_calls") or []):
                                        fn = tc.get("function") or {}
                                        tool_calls_acc[idx] = {
                                            "name": fn.get("name", ""),
                                            "args": [fn.get("arguments", "")],
                                        }
                                if evt.get("timings"):
                                    timings_data = evt["timings"]
                                if evt.get("usage"):
                                    usage_data = evt["usage"]
                            except Exception:
                                pass

            if is_chat:
                t_end = time.perf_counter()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ttft_s = (t_first_token - t_start) if t_first_token else max(t_end - t_start, 0.001)

                think_text = "".join(think_parts)
                out_text = "".join(out_parts)
                think_chars = len(think_text)
                out_chars = len(out_text)

                if tool_calls_acc:
                    ordered = [tool_calls_acc[k] for k in sorted(tool_calls_acc)]
                    detail_list = []
                    sig_list = []
                    for item in ordered:
                        t_name = item["name"] or "tool"
                        t_args = "".join(item["args"])
                        detail_list.append(f"{t_name}({t_args})")
                        sig_list.append(f"{t_name}:{t_args}")
                    tools_detail = " ; ".join(detail_list)
                    sig_raw = "|".join(sig_list)
                else:
                    tools_detail = "respuesta_final"
                    sig_raw = f"TEXT:{out_text.strip()}"

                alerts = []
                prev_err = bool(meta_in and 'ERR,' in meta_in.get('trigger_info', ''))
                loop_flag = check_loop(sig_raw, prev_err).strip()
                if loop_flag:
                    alerts.append(loop_flag)
                    with STATE_LOCK:
                        STATE["force_break_loop"] = True
                if finish_reason == "length":
                    alerts.append("[ALERTA CORTADO POR MAX_TOKENS]")
                if think_chars > 12000 and out_chars == 0 and not tool_calls_acc:
                    alerts.append("[ALERTA THINK EXCESIVO SIN SALIDA]")
                alerts_str = (" " + " ".join(alerts)) if alerts else ""

                batch = []
                label = meta_in["label"] if meta_in else "[PASO]"
                if meta_in:
                    cop_flag = "OK" if meta_in["has_copilot"] else "NO"
                    md_str = ",".join(meta_in["md_files"]) if meta_in["md_files"] else "ninguno"
                    code_str = ",".join(meta_in["code_files"][:25]) if meta_in["code_files"] else "ninguno"
                    r = meta_in["roles"]
                    rc = meta_in["role_chars"]
                    ht = meta_in["hist_tools"]
                    ht_str = ",".join(f"{k}:{v}" for k, v in sorted(ht.items())) if ht else "ninguna"
                    rp = meta_in["req_params"]
                    sc_kept = meta_in["tools_schema_count"]
                    sc_orig = meta_in["orig_schema_count"]
                    sc_saved = meta_in["saved_schema_chars"]
                    sc_tag = f"{sc_kept}/{sc_orig} (-{sc_saved}c)" if sc_orig > sc_kept else f"{sc_kept}"
                    batch.append(
                        f"[{now_str}] {label} [SYS-CHECK: copilot-instructions={cop_flag} ({meta_in['sys_chars']}c) | "
                        f"md_files={md_str} | code_files={code_str}]"
                    )
                    batch.append(
                        f"[{now_str}] {label} [CTX: {ctx_tokens_sent} tok | roles(sys={r['sys']}/{rc['sys']}c, "
                        f"u={r['user']}/{rc['user']}c, a={r['asst']}/{rc['asst']}c, t={r['tool']}/{rc['tool']}c) | "
                        f"schemas={sc_tag} | hist_tools={ht_str} | "
                        f"params(temp={rp.get('temp')},top_p={rp.get('top_p')},max={rp.get('max_tok')},stream={rp.get('stream')},think={rp.get('think')})] "
                        f"{meta_in['trigger_info']}"
                    )

                if timings_data:
                    p_n = timings_data.get("prompt_n", 0)
                    p_ms = timings_data.get("prompt_ms", 0.0)
                    p_tps = timings_data.get("prompt_per_second", 0.0)
                    g_n = timings_data.get("predicted_n", 0)
                    g_ms = timings_data.get("predicted_ms", 0.0)
                    g_tps = timings_data.get("predicted_per_second", 0.0)
                    real_ctx = usage_data.get("prompt_tokens", ctx_tokens_sent) if usage_data else ctx_tokens_sent
                    kv_hit = 0.0
                    if isinstance(real_ctx, int) and real_ctx > 0 and real_ctx >= p_n:
                        kv_hit = ((real_ctx - p_n) / real_ctx) * 100.0
                    batch.append(
                        f"[{now_str}] {label} [RUN] KV-Hit: {kv_hit:.1f}% | TTFT: {ttft_s:.2f}s | "
                        f"Prompt: {p_n}/{real_ctx} tok ({p_ms/1000:.2f}s @ {p_tps:.1f} t/s) | "
                        f"Gen: {g_n} tok ({g_ms/1000:.2f}s @ {g_tps:.1f} t/s) | Finish: {finish_reason}{alerts_str}"
                    )
                else:
                    gen_s = max(t_end - (t_first_token or t_start), 0.001)
                    p_n = usage_data.get("prompt_tokens", "?") if usage_data else "?"
                    g_n = usage_data.get("completion_tokens", stream_chunks) if usage_data else stream_chunks
                    g_tps = (g_n / gen_s) if isinstance(g_n, int) and g_n > 0 else 0.0
                    batch.append(
                        f"[{now_str}] {label} [RUN] TTFT: {ttft_s:.2f}s | Prompt: {p_n}/{ctx_tokens_sent} tok | "
                        f"Gen: {g_n} tok ({gen_s:.2f}s @ {g_tps:.1f} t/s) | Finish: {finish_reason}{alerts_str}"
                    )

                batch.append(f"[{now_str}] {label} [TOOLS-CALL] {tools_detail}")
                if think_text:
                    batch.append(f"[{now_str}] {label} [THINK-FULL ({think_chars}c)] >>>\n{think_text}\n<<< [END-THINK]")
                if out_text:
                    batch.append(f"[{now_str}] {label} [OUT-FULL ({out_chars}c)] >>>\n{out_text}\n<<< [END-OUT]")

                log_batch(batch)

        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass
        except Exception as e:
            try:
                msg = json.dumps({"error": str(e)}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def do_OPTIONS(self):
        self._forward()


if __name__ == "__main__":
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass
    server = SilentHTTPServer(("127.0.0.1", 1234), ProxyHandler)
    log_line(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PROXY] Escuchando en http://127.0.0.1:1234 -> http://127.0.0.1:1235 | Log: {LOG_PATH}")
    server.serve_forever()

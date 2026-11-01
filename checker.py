#!/usr/bin/env python3
import os
import re
import sys
import csv
import subprocess
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

import yaml


# ---------- Парсинг имён файлов ----------
NAME_RE = re.compile(
    r"^(?P<fio>[^-]+)-(?P<rest>.*?)"
    r"(?:live|home)_problem_(?P<problem>\d+)"
    r"_attempt_(?P<attempt>\d+)",
    re.IGNORECASE,
)


@dataclass
class Attempt:
    file: Path
    fio: str                 # Фамилия_Имя_Отчество
    surname: str             # первое слово
    kind: str                # 'live' | 'home'
    problem: int
    attempt_no: int
    success: bool = False
    error: str = ""


# ---------- Парсинг answers ----------
def parse_answer_file(path: Path):
    """Возвращает список (stdin, expected_stdout)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    # Разбиваем по маркеру ---CASE---
    chunks = re.split(r"^\s*---CASE---\s*$", text, flags=re.MULTILINE)
    cases = []
    for ch in chunks:
        if not ch.strip():
            continue
        m_in = re.search(r"^\s*---IN---\s*$(.*?)^\s*---OUT---\s*$(.*)\Z",
                         ch, flags=re.MULTILINE | re.DOTALL)
        if not m_in:
            continue
        stdin_data = m_in.group(1)
        stdout_data = m_in.group(2)
        # нормализуем: убираем возможные хвостовые пустые строки, но оставляем \n
        cases.append((stdin_data, stdout_data))
    return cases


def normalize(s: str) -> str:
    # сравниваем с точностью до хвостовых пробелов/переводов строк в конце
    return "\n".join(line.rstrip() for line in s.strip().splitlines())


# ---------- Компиляция и запуск ----------
def compile_cpp(src: Path, exe: Path, cfg) -> Optional[str]:
    cmd = [cfg["compiler"], *cfg.get("compile_flags", []), str(src), "-o", str(exe)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=cfg.get("compile_timeout_sec", 20))
    except subprocess.TimeoutExpired:
        return "compile timeout"
    if r.returncode != 0:
        return "compile error:\n" + (r.stderr or "")[:2000]
    return None


def run_case(exe: Path, stdin_data: str, expected: str, cfg) -> bool:
    try:
        r = subprocess.run(
            [str(exe)],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=cfg.get("timeout_sec", 2),
        )
    except subprocess.TimeoutExpired:
        return False
    if r.returncode != 0:
        return False
    return normalize(r.stdout) == normalize(expected)


# ---------- Сбор файлов студентов ----------
def collect_attempts(students_dir: Path, problems: set[int], name_filter):
    attempts: dict[tuple[str, int], list[Attempt]] = {}
    for prob_dir in sorted(students_dir.iterdir()):
        if not prob_dir.is_dir():
            continue
        for f in sorted(prob_dir.glob("*.cpp")):
            m = NAME_RE.match(f.stem)
            if not m:
                print(f"[warn] не распознано имя: {f.name}", file=sys.stderr)
                continue
            problem = int(m.group("problem"))
            if problem not in problems:
                continue
            fio = m.group("fio")
            surname = fio.split("_")[0]
            if name_filter != "all":
                if surname not in name_filter and fio not in name_filter:
                    continue
            kind = "live" if "live" in m.group("rest").lower() or "live" in f.stem.lower() else "home"
            # уточним: ищем именно подстроку
            low = f.stem.lower()
            kind = "live" if "live" in low else ("home" if "home" in low else kind)
            att = Attempt(
                file=f, fio=fio, surname=surname,
                kind=kind, problem=problem,
                attempt_no=int(m.group("attempt")),
            )
            attempts.setdefault((fio, problem), []).append(att)
    return attempts


# ---------- Проверка ----------
def check_all(cfg, attempts):
    work = Path(cfg["work_dir"])
    work.mkdir(exist_ok=True)

    # кэш тестов по задаче
    tests_cache: dict[int, list] = {}

    for (fio, problem), atts in attempts.items():
        ans_file = Path(cfg["answers_dir"]) / f"{problem}.txt"
        if problem not in tests_cache:
            if not ans_file.exists():
                print(f"[warn] нет ответов для задачи {problem}", file=sys.stderr)
                tests_cache[problem] = []
            else:
                tests_cache[problem] = parse_answer_file(ans_file)
        tests = tests_cache[problem]

        # Сортируем попытки: сначала live по возрастанию attempt, потом home
        atts.sort(key=lambda a: (0 if a.kind == "live" else 1, a.attempt_no))

        for att in atts:
            if not tests:
                att.error = "no tests"
                continue
            exe = work / f"{att.file.stem}.out"
            err = compile_cpp(att.file, exe, cfg)
            if err:
                att.error = err
                continue
            ok = True
            for i, (inp, out) in enumerate(tests, 1):
                if not run_case(exe, inp, out, cfg):
                    att.error = f"test {i} failed"
                    ok = False
                    break
            att.success = ok

    return attempts


# ---------- Формирование CSV ----------
def build_csv(cfg, attempts):
    # множество студентов
    students = sorted({fio for (fio, _) in attempts.keys()})

    # для каждого студента: были ли live-попытки по задаче
    live_attempted: dict[tuple[str, int], bool] = {}
    for (fio, problem), atts in attempts.items():
        live_attempted[(fio, problem)] = any(a.kind == "live" for a in atts)

    rows = []
    for fio in students:
        row = {"student": fio}
        total = 0
        for problem in sorted(cfg["problems"]):
            key = (fio, problem)
            atts = attempts.get(key, [])
            cell = {
                "solved": "no",
                "where": "",
                "attempt": "",
                "counted": "no",
            }
            if atts:
                live_ok = [a for a in atts if a.kind == "live" and a.success]
                home_ok = [a for a in atts if a.kind == "home" and a.success]
                if live_ok:
                    best = min(live_ok, key=lambda a: a.attempt_no)
                    cell.update(solved="yes", where="live",
                                attempt=f"attempt_{best.attempt_no}")
                    counted = True
                elif home_ok:
                    best = min(home_ok, key=lambda a: a.attempt_no)
                    cell.update(solved="yes", where="home",
                                attempt=f"attempt_{best.attempt_no}")
                    counted = live_attempted.get(key, False)
                else:
                    cell.update(solved="no")
                    counted = False
                cell["counted"] = "yes" if counted else "no"
                if counted:
                    total += 1
            row[f"p{problem}_solved"] = cell["solved"]
            row[f"p{problem}_where"] = cell["where"]
            row[f"p{problem}_attempt"] = cell["attempt"]
            row[f"p{problem}_counted"] = cell["counted"]
        row["total_counted"] = total
        rows.append(row)
    return rows


def write_csv(cfg, rows):
    out_dir = Path(cfg["results_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"results_{ts}.csv"

    problems = sorted(cfg["problems"])
    header = ["student"]
    for p in problems:
        header += [f"p{p}_solved", f"p{p}_where", f"p{p}_attempt", f"p{p}_counted"]
    header += ["total_counted"]

    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\nCSV записан: {out}")


# ---------- main ----------
def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.yml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # нормализация problems: поддержим "all"
    students_dir = Path(cfg["students_dir"])
    problems = cfg.get("problems", "all")
    if problems == "all":
        # все problem_N в students/
        problems = set()
        for d in students_dir.iterdir():
            m = re.match(r"problem_(\d+)$", d.name)
            if d.is_dir() and m:
                problems.add(int(m.group(1)))
    else:
        problems = set(int(p) for p in problems)
    cfg["problems"] = sorted(problems)

    name_filter = cfg.get("students", "all")
    if name_filter == "all":
        name_filter = "all"
    else:
        name_filter = set(name_filter)

    attempts = collect_attempts(students_dir, problems, name_filter)
    if not attempts:
        print("Не найдено попыток по фильтру.", file=sys.stderr)
        sys.exit(1)

    attempts = check_all(cfg, attempts)

    # лог по каждой попытке (опционально — печатаем в консоль)
    print("\n=== Детали проверок ===")
    for (fio, problem), atts in sorted(attempts.items()):
        for a in atts:
            status = "OK " if a.success else "FAIL"
            extra = "" if a.success else f" ({a.error[:80]})"
            print(f"[{status}] {fio} | p{problem} | {a.kind} attempt_{a.attempt_no}{extra}")

    rows = build_csv(cfg, attempts)
    write_csv(cfg, rows)


if __name__ == "__main__":
    main()
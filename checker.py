#!/usr/bin/env python3
"""Проверка решений студентов: students/problem_N/*.cpp против answers/N.txt.

Дополнительно (независимо от прохождения тестов) для каждого студента
проверяется наличие `#include <vector>` и `#include <string>` во всех его
попытках по всем задачам — «да/нет» плюс в скольких попытках заголовок есть.

Бинарники собираются во временной папке (tempfile.TemporaryDirectory),
которая удаляется при выходе: на диске не остаётся артефактов сборки.
"""

import csv
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from tqdm import tqdm


# ---------- Парсинг имён файлов ----------
NAME_RE = re.compile(
    r"^(?P<fio>[^-]+)-(?P<rest>.*?)"
    r"(?:live|home)_problem_(?P<problem>\d+)"
    r"_attempt_(?P<attempt>\d+)",
    re.IGNORECASE,
)


@dataclass
class Case:
    stdin: str
    expected: str
    line: int                    # строка ---IN--- в файле ответов


@dataclass
class Attempt:
    file: Path
    fio: str                     # Фамилия_Имя_Отчество
    surname: str                 # первое слово
    kind: str                    # 'live' | 'home'
    problem: int
    attempt_no: int
    success: bool = False
    error: str = ""
    tests_passed: int = 0
    tests_total: int = 0
    inc_vector: bool = False     # есть #include <vector> в исходнике
    inc_string: bool = False     # есть #include <string> в исходнике


class AnswerFormatError(Exception):
    """Файл ответов структурно битый — проверять по нему нельзя."""


# ---------- Парсинг answers ----------
IN_MARK = "---IN---"
OUT_MARK = "---OUT---"
CASE_MARK = "---CASE---"


def _join_block(block: list[str]) -> str:
    """Склеивает блок в строку stdin/expected, отбрасывая хвостовые пустые строки."""
    while block and not block[-1].strip():
        block.pop()
    if not block:
        return ""
    return "\n".join(block) + "\n"


def parse_answer_file(path: Path) -> list[Case]:
    """Разбирает файл ответов построчно:

        ---CASE---
        ---IN---
        <stdin (может быть пустым)>
        ---OUT---
        <ожидаемый stdout (может быть пустым)>

    В отличие от старой regex-версии, разбор идёт по маркерам-строкам, поэтому:
      * кейс с пустым ожидаемым выводом поддерживается (---OUT--- сразу за входом);
      * пропущенный маркер ---OUT---/---CASE---/---IN--- не «съедается» молча:
        это ошибка формата с номером строки. Иначе вход следующего кейса
        приклеивался к предыдущему, и верное решение получало FAIL.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    cases: list[Case] = []
    state = "outside"            # outside | in | out
    in_lines: list[str] = []
    out_lines: list[str] = []
    in_line_no = 0

    def finish_case() -> None:
        nonlocal in_lines, out_lines, state
        cases.append(Case(stdin=_join_block(in_lines),
                          expected=_join_block(out_lines),
                          line=in_line_no))
        in_lines, out_lines = [], []
        state = "outside"

    for no, raw in enumerate(lines, 1):
        token = raw.strip()
        if token == CASE_MARK:
            if state == "in":
                raise AnswerFormatError(
                    f"{path}:{no}: кейс из строки {in_line_no} оборван маркером "
                    f"---CASE--- без ---OUT---"
                )
            if state == "out":
                finish_case()
            continue

        if token == IN_MARK:
            if state == "in":
                raise AnswerFormatError(
                    f"{path}:{no}: второй ---IN--- подряд (кейс из строки {in_line_no} "
                    f"так и не получил ---OUT---)"
                )
            if state == "out":
                finish_case()
            state = "in"
            in_line_no = no
            continue

        if token == OUT_MARK:
            if state != "in":
                raise AnswerFormatError(
                    f"{path}:{no}: ---OUT--- без предшествующего ---IN---"
                )
            state = "out"
            continue

        if state == "in":
            in_lines.append(raw)
        elif state == "out":
            out_lines.append(raw)
        elif token:
            raise AnswerFormatError(f"{path}:{no}: данные вне кейса: {raw!r}")

    if state == "in":
        raise AnswerFormatError(
            f"{path}: кейс из строки {in_line_no} без ---OUT--- (конец файла)"
        )
    if state == "out":
        finish_case()

    if not cases:
        raise AnswerFormatError(f"{path}: не найдено ни одного кейса")

    # Дополнительный инвариант: маркеры должны быть сбалансированы.
    # Если строк ---CASE---/---IN---/---OUT--- поровну и столько же кейсов,
    # значит из файла не потеряна ни одна служебная строка.
    for mark in (CASE_MARK, IN_MARK, OUT_MARK):
        cnt = sum(1 for raw in lines if raw.strip() == mark)
        if cnt != len(cases):
            raise AnswerFormatError(
                f"{path}: маркеров {mark} — {cnt}, а кейсов — {len(cases)}: "
                f"в файле потеряны служебные строки"
            )
    return cases


# ---------- Инклуды ----------
INCLUDE_RE = re.compile(r'^#\s*include\s*[<"]([^>"]+)[>"]', re.IGNORECASE)

# Какие заголовки отслеживаем отдельно (имя в нижнем регистре -> человеческое имя)
WATCHED_INCLUDES = ("vector", "string")


def parse_includes(src: Path) -> set[str]:
    """Множество подключённых заголовков (имена в нижнем регистре).

    Понимает `#include <vector>`, `#include<vector>`, `#include  "vector"`,
    хвостовые `;`/комментарии и регистр (`<Vector>`). Закомментированные
    директивы (`//#include <vector>`) не учитываются — иначе студент получал бы
    «да» за закомментированную строку.
    """
    text = src.read_text(encoding="utf-8", errors="replace")
    found: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        m = INCLUDE_RE.match(line)
        if m:
            found.add(m.group(1).strip().lower())
    return found


def normalize(s: str) -> str:
    """Нормализация для сравнения: незначащие хвостовые пробелы в строках
    и хвостовые пустые строки. Ведущие пробелы значимы — вывод бывает
    выровнен пробелами (см. задачу про пирамиду)."""
    lines = s.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line.rstrip() for line in lines)


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


def diff_message(expected: str, got: str) -> str:
    el, gl = expected.split("\n"), got.split("\n")
    for i in range(max(len(el), len(gl))):
        e = el[i] if i < len(el) else "<нет строки>"
        g = gl[i] if i < len(gl) else "<нет строки>"
        if e != g:
            return (f"строка {i + 1}: ожидалось {e!r}, получено {g!r} "
                    f"(строк: ожидалось {len(el)}, получено {len(gl)})")
    return "вывод совпадает"


def run_case(exe: Path, case: Case, cfg) -> Optional[str]:
    """None — тест пройден, иначе текст ошибки."""
    try:
        r = subprocess.run(
            [str(exe)],
            input=case.stdin,
            capture_output=True,
            text=True,
            timeout=cfg.get("timeout_sec", 2),
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    if r.returncode != 0:
        return f"runtime error (код возврата {r.returncode})"
    expected = normalize(case.expected)
    got = normalize(r.stdout)
    if expected == got:
        return None
    return diff_message(expected, got)


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
            low = f.stem.lower()
            kind = "live" if "live" in low else ("home" if "home" in low else "unknown")
            if kind == "unknown":
                print(f"[warn] не понятно live/home: {f.name}", file=sys.stderr)
            incs = parse_includes(f)
            att = Attempt(
                file=f, fio=fio, surname=surname,
                kind=kind, problem=problem,
                attempt_no=int(m.group("attempt")),
                inc_vector="vector" in incs,
                inc_string="string" in incs,
            )
            attempts.setdefault((fio, problem), []).append(att)
    return attempts


# ---------- Загрузка тестов ----------
def load_tests(cfg, problems) -> tuple[dict[int, list[Case]], list[str]]:
    tests: dict[int, list[Case]] = {}
    errors: list[str] = []
    print("=== Набор тестов ===")
    for problem in sorted(problems):
        ans_file = Path(cfg["answers_dir"]) / f"{problem}.txt"
        if not ans_file.exists():
            errors.append(f"{ans_file}: файл ответов не найден")
            continue
        try:
            cases = parse_answer_file(ans_file)
        except AnswerFormatError as e:
            errors.append(str(e))
            continue
        tests[problem] = cases
        empty = sum(1 for c in cases if not normalize(c.expected))
        extra = f", из них с пустым выводом: {empty}" if empty else ""
        print(f"  p{problem}: {len(cases)} кейсов ({ans_file}){extra}")
    return tests, errors


# ---------- Проверка ----------
def check_attempt(att: Attempt, tests: list[Case], cfg, work: Path, ans_name: str) -> None:
    att.tests_total = len(tests)
    exe = work / f"{att.file.stem}.out"
    err = compile_cpp(att.file, exe, cfg)
    if err:
        att.error = err
        return
    for i, case in enumerate(tests, 1):
        err = run_case(exe, case, cfg)
        if err:
            att.error = f"тест {i}/{len(tests)} ({ans_name}:{case.line}): {err}"
            return
        att.tests_passed = i
    att.success = True


def check_all(cfg, attempts, tests_by_problem, work: Path):
    """Прогоняет все попытки, складывая бинарники во временную папку `work`.

    Прогресс — один бар на все попытки всех студентов и всех задач.
    """
    queue: list[tuple[str, int, Attempt]] = []
    for (fio, problem), atts in attempts.items():
        # сначала live по возрастанию attempt, потом home
        atts.sort(key=lambda a: (0 if a.kind == "live" else 1, a.attempt_no))
        queue.extend((fio, problem, att) for att in atts)

    bar = tqdm(queue, desc="Проверка решений", unit="поп",
               dynamic_ncols=True, leave=True)
    for fio, problem, att in bar:
        bar.set_postfix_str(f"p{problem} {att.surname} {att.kind} "
                            f"#{att.attempt_no}", refresh=False)
        check_attempt(att, tests_by_problem[problem], cfg, work,
                      f"answers/{problem}.txt")
        bar.set_postfix_str(f"p{problem} {att.surname} {att.kind} "
                            f"#{att.attempt_no} {'OK' if att.success else 'FAIL'}",
                            refresh=False)
    bar.close()
    return attempts


def group_by_student(attempts) -> dict[str, list[Attempt]]:
    """Все попытки студента по всем задачам в одном списке."""
    by_student: dict[str, list[Attempt]] = {}
    for (fio, _), atts in attempts.items():
        by_student.setdefault(fio, []).extend(atts)
    return by_student


def include_summary(atts: list[Attempt]) -> dict[str, str]:
    """Сводка по инклудам для набора попыток (по всем задачам студента).

    `has_*` — да/нет: встретился ли заголовок хотя бы в одной попытке,
    `*_attempts` — в скольких попытках из скольких он есть.
    """
    total = len(atts)
    out = {}
    for header in WATCHED_INCLUDES:
        flag = f"inc_{header}"
        cnt = sum(1 for a in atts if getattr(a, flag))
        out[f"has_{header}"] = "да" if cnt else "нет"
        out[f"{header}_attempts"] = f"{cnt}/{total}" if total else "0/0"
    return out


# ---------- Формирование CSV ----------
def build_csv(cfg, attempts):
    students = sorted({fio for (fio, _) in attempts.keys()})
    by_student = group_by_student(attempts)

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
                "tests": "",
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
                # сколько тестов реально прогнано в лучшей попытке
                best_tests = max(atts, key=lambda a: (a.success, a.tests_passed))
                cell["tests"] = f"{best_tests.tests_passed}/{best_tests.tests_total}"
                cell["counted"] = "yes" if counted else "no"
                if counted:
                    total += 1
            row[f"p{problem}_solved"] = cell["solved"]
            row[f"p{problem}_where"] = cell["where"]
            row[f"p{problem}_attempt"] = cell["attempt"]
            row[f"p{problem}_counted"] = cell["counted"]
            row[f"p{problem}_tests"] = cell["tests"]
        row["total_counted"] = total
        # отдельная проверка инклудов: по всем попыткам всех задач студента
        row.update(include_summary(by_student.get(fio, [])))
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
        header += [f"p{p}_solved", f"p{p}_where", f"p{p}_attempt",
                   f"p{p}_counted", f"p{p}_tests"]
    header += ["total_counted"]
    for h in WATCHED_INCLUDES:
        header += [f"has_{h}", f"{h}_attempts"]

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

    # 1. Сначала тесты: без валидных ответов проверять бессмысленно.
    tests_by_problem, errors = load_tests(cfg, problems)
    if errors:
        print("\n[error] Файлы ответов битые, проверка отменена:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print("\nПодсказка: каждый кейс должен выглядеть так:\n"
              "  ---CASE---\n  ---IN---\n  <вход>\n  ---OUT---\n  <ожидаемый вывод>\n"
              "Пустой вывод допустим, но строка ---OUT--- обязательна.", file=sys.stderr)
        sys.exit(2)

    # 2. Затем решения студентов.
    attempts = collect_attempts(students_dir, problems, name_filter)
    if not attempts:
        print("Не найдено попыток по фильтру.", file=sys.stderr)
        sys.exit(1)

    # Бинарники собираем во временной папке: после проверки она удаляется
    # вместе со всеми артефактами, на диске ничего не остаётся.
    with tempfile.TemporaryDirectory(prefix="students-check-build-") as tmp:
        attempts = check_all(cfg, attempts, tests_by_problem, Path(tmp))

    # лог по каждой попытке
    print("\n=== Детали проверок ===")
    for (fio, problem), atts in sorted(attempts.items()):
        for a in atts:
            status = "OK  " if a.success else "FAIL"
            passed = f"{a.tests_passed}/{a.tests_total}"
            extra = "" if a.success else f" — {a.error}"
            print(f"[{status}] {fio} | p{problem} | {a.kind} "
                  f"attempt_{a.attempt_no} ({passed}) "
                  f"| vector={'да' if a.inc_vector else 'нет'} "
                  f"string={'да' if a.inc_string else 'нет'}{extra}")

    # сводка по инклудам: по всем попыткам всех задач
    print("\n=== Инклуды vector/string (по всем попыткам всех задач) ===")
    by_student = group_by_student(attempts)
    for fio in sorted(by_student):
        atts = by_student[fio]
        summary = include_summary(atts)
        print(f"{fio}: vector={summary['has_vector']} "
              f"({summary['vector_attempts']} попыток), "
              f"string={summary['has_string']} "
              f"({summary['string_attempts']} попыток)")

    rows = build_csv(cfg, attempts)
    write_csv(cfg, rows)


if __name__ == "__main__":
    main()

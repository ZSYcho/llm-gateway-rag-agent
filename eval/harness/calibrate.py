"""Judge calibration: compare the judge's key-point verdicts with a human's, blind.

    python -m eval.harness.calibrate sample [--tags base-routed,base-cloud,base-local] [--n 30]
    (fill in the human_verdict column of eval/results/calibration.xlsx, or of the .csv copy,
     with yes / partial / no)
    python -m eval.harness.calibrate score             # agreement and Cohen's kappa

The sheet does not show the judge's verdicts, so they cannot anchor the human
ones; they are kept in calibration.key.json and only joined in by ``score``.
"""

import argparse
import csv
import json
import random
from collections import Counter

from eval.harness.paths import EVAL_DIR

LABELS = ("yes", "partial", "no")
RESULTS = EVAL_DIR / "results"
CSV_PATH = RESULTS / "calibration.csv"
XLSX_PATH = RESULTS / "calibration.xlsx"
KEY_PATH = RESULTS / "calibration.key.json"
COLUMNS = ("id", "question", "key_point", "answer", "human_verdict")


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_xlsx(items: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.worksheet.datavalidation import DataValidation

    book = Workbook()
    sheet = book.active
    sheet.title = "calibration"
    sheet.append(COLUMNS)
    for item in items:
        sheet.append([item[column] for column in COLUMNS])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    widths = {"A": 30, "B": 36, "C": 36, "D": 90, "E": 15}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    # Only the three labels can be entered in the verdict column.
    choice = DataValidation(type="list", formula1='"yes,partial,no"', allow_blank=True)
    choice.add(f"E2:E{len(items) + 1}")
    sheet.add_data_validation(choice)
    sheet.freeze_panes = "A2"
    book.save(XLSX_PATH)


def sample(tags: list[str], n: int, seed: int = 11) -> None:
    from eval.harness import dataset

    cases = dataset.load_all()
    items = []
    for tag in tags:
        out_dir = RESULTS / tag
        runs = {(r["case_id"], r["rep"]): r for r in _jsonl(out_dir / "runs.jsonl")}
        for judged in _jsonl(out_dir / "judged.jsonl"):
            case = cases[judged["case_id"]]
            run = runs[(judged["case_id"], judged["rep"])]
            for point in judged["key_points"]["points"]:
                if 1 <= point["index"] <= len(case["key_points"]):
                    items.append({
                        "id": f"{tag}/{judged['case_id']}#r{judged['rep']}#{point['index']}",
                        "question": run["turns"][-1]["question"],
                        "key_point": case["key_points"][point["index"] - 1],
                        "answer": run["answer"],
                        "judge_verdict": point["verdict"],
                        "human_verdict": "",
                    })
    # Hand out the n slots to the verdicts in turn, so the rare "partial" and
    # "no" verdicts are checked as well as the common "yes".
    by_verdict: dict[str, list] = {}
    for item in items:
        by_verdict.setdefault(item["judge_verdict"], []).append(item)
    slots = dict.fromkeys(by_verdict, 0)
    while sum(slots.values()) < min(n, len(items)):
        for verdict in sorted(by_verdict):
            if slots[verdict] < len(by_verdict[verdict]) and sum(slots.values()) < n:
                slots[verdict] += 1
    rng = random.Random(seed)
    chosen = [item for verdict, group in by_verdict.items() for item in rng.sample(group, slots[verdict])]
    rng.shuffle(chosen)

    KEY_PATH.write_text(json.dumps({i["id"]: i["judge_verdict"] for i in chosen}, indent=1), encoding="utf-8")
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(chosen)
    _write_xlsx(chosen)
    print(f"{len(chosen)} items {dict(Counter(i['judge_verdict'] for i in chosen))}")
    print(f"  fill in: {XLSX_PATH}  (or the CSV copy {CSV_PATH.name})")


def _human_verdicts() -> dict[str, str]:
    """Labels from the workbook and the CSV copy; the workbook wins where both are filled."""
    labels: dict[str, str] = {}
    if CSV_PATH.exists():
        with CSV_PATH.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                labels[row["id"]] = (row.get("human_verdict") or "").strip().lower()
    if XLSX_PATH.exists():
        from openpyxl import load_workbook

        sheet = load_workbook(XLSX_PATH, read_only=True).active
        header = [cell for cell in next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))]
        id_col, verdict_col = header.index("id"), header.index("human_verdict")
        for row in sheet.iter_rows(min_row=2, values_only=True):
            if row[id_col] and row[verdict_col]:
                labels[row[id_col]] = str(row[verdict_col]).strip().lower()
    return {key: value for key, value in labels.items() if value in LABELS}


SCORES = {"yes": 1.0, "partial": 0.5, "no": 0.0}


def _case_flips(item_id: str, human: str) -> bool:
    """Whether the human verdict on this key point would change the case's correct/incorrect outcome."""
    from eval.harness import dataset

    tag, rest = item_id.split("/")
    case_id, rep, point = rest.split("#")
    case = dataset.load_all()[case_id]
    judged = next(r for r in _jsonl(RESULTS / tag / "judged.jsonl") if r["case_id"] == case_id and f"r{r['rep']}" == rep)
    key_points = judged["key_points"]
    verdicts = {p["index"]: p["verdict"] for p in key_points["points"]}
    verdicts[int(point)] = human
    coverage = sum(SCORES[verdicts.get(i, "no")] for i in range(1, len(case["key_points"]) + 1)) / len(case["key_points"])
    correct = coverage >= 0.8 and not key_points["violated_must_not"] and key_points["response_type"] != "refused"
    return correct != key_points["correct"]


def agreement() -> dict | None:
    """Agreement between the judge and the human on the labelled items, or None before labelling."""
    if not KEY_PATH.exists():
        return None
    judge = json.loads(KEY_PATH.read_text(encoding="utf-8"))
    human = {key: verdict for key, verdict in _human_verdicts().items() if key in judge}
    if not human:
        return None
    pairs = [(judge[key], verdict) for key, verdict in human.items()]
    n = len(pairs)
    observed = sum(j == h for j, h in pairs) / n
    judge_counts, human_counts = Counter(j for j, _ in pairs), Counter(h for _, h in pairs)
    expected = sum(judge_counts[label] * human_counts[label] for label in LABELS) / (n * n)
    disagreements = [(key, judge[key], verdict) for key, verdict in human.items() if judge[key] != verdict]
    return {
        "n": n,
        "total": len(judge),
        "observed": observed,
        "kappa": (observed - expected) / (1 - expected) if expected < 1 else 1.0,
        # Binary view: does the answer cover the point at all?
        "binary": sum((j == "no") == (h == "no") for j, h in pairs) / n,
        "disagreements": disagreements,
        "judge_stricter": sum(SCORES[j] < SCORES[h] for _, j, h in disagreements),
        "judge_lenient": sum(SCORES[j] > SCORES[h] for _, j, h in disagreements),
        "case_flips": sum(_case_flips(key, h) for key, _, h in disagreements),
    }


def score() -> None:
    stats = agreement()
    if stats is None:
        raise SystemExit("no human verdicts yet")
    print(f"n={stats['n']}/{stats['total']}  exact agreement={stats['observed']:.1%}  Cohen's kappa={stats['kappa']:.2f}  "
          f"covered/not-covered agreement={stats['binary']:.1%}")
    print(f"  judge stricter on {stats['judge_stricter']}, more lenient on {stats['judge_lenient']}; "
          f"cases whose correct/incorrect outcome would change: {stats['case_flips']}")
    for key, judge, human in stats["disagreements"]:
        print(f"  {key}: judge={judge} human={human}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m eval.harness.calibrate")
    parser.add_argument("action", choices=("sample", "score"))
    parser.add_argument("--tags", default="base-routed,base-cloud,base-local")
    parser.add_argument("--n", type=int, default=30)
    args = parser.parse_args()
    sample(args.tags.split(","), args.n) if args.action == "sample" else score()


if __name__ == "__main__":
    main()

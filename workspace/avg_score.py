"""Read workspace/data.csv, compute average of the 'score' column, print result."""

import csv
from pathlib import Path

# Use a relative path to find data.csv in the workspace
DATA_PATH = Path("data.csv")

scores: list[float] = []
with DATA_PATH.open(newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        # Ensure the score is treated as a float
        scores.append(float(row["score"]))

# Calculate average, handle empty list
average = sum(scores) / len(scores) if scores else float("nan")
print(f"Average score: {average}")

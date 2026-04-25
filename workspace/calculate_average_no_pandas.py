import csv

# Read the CSV file
with open('data.csv', 'r') as file:
    reader = csv.DictReader(file)
    scores = []
    for row in reader:
        scores.append(float(row['score']))

# Calculate the average of the 'score' column
average_score = sum(scores) / len(scores)

# Print the result
print(f"The average score is: {average_score}")

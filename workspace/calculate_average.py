import pandas as pd

# Read the CSV file
df = pd.read_csv('data.csv')

# Calculate the average of the 'score' column
average_score = df['score'].mean()

# Print the result
print(f"The average score is: {average_score}")

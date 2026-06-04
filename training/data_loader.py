import pandas as pd
import os

TRAINING_DATA_PATH = "training.csv"

def load_data(csv_path: str = None) -> pd.DataFrame:
    """
    Load data from a CSV file. Returns a DataFrame or None if loading fails.
    """
    path = csv_path if csv_path else TRAINING_DATA_PATH
    try:
        df = pd.read_csv(path)
        # When adding changes to pipeline, validate with smaller sample size
        # df = pd.concat([
        #     df[df['appointment_attended'] == True].sample(frac=0.005, random_state=42),
        #     df[df['appointment_attended'] == False].sample(frac=0.005, random_state=42)
        # ])
        print(f"Loaded data with shape: {df.shape}")
        return df
    except FileNotFoundError:
        print(f"Error: File '{path}' not found.")
    except Exception as e:
        print(f"An error occurred loading data: {e}")
    return None

import pandas as pd

MIN_VALID_BIRTH_YEAR = 1980

def convert_dates(df: pd.DataFrame, date_columns: list) -> pd.DataFrame:
    """
    Convert specified columns to datetime.
    """
    for col in date_columns:
        try:
            df[col] = pd.to_datetime(df[col])
        except Exception as e:
            print(f"Error converting {col} to datetime: {e}")
    return df


def fix_data_types(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert columns to appropriate dtypes (bool, int, category, float).
    Uses pandas nullable types where appropriate to handle None values.
    """
    for col in [
        'chronic_patient', 'patient_birth_year', 'distance_to_appointment_km',
        'appointment_time_hour', 'doctor_id', 'doctor_type_id',
        'service_type_id', 'appointment_attended', 'appointment_status', 'appointment_approved'
    ]:
        try:
            if col in ['chronic_patient', 'appointment_attended', 'appointment_approved']:
                df[col] = df[col].astype('bool')
            elif col in ['appointment_time_hour', 'appointment_status']:
                df[col] = df[col].astype('int64')
            elif col == 'patient_birth_year':
                # Use pandas nullable integer type to handle None values
                df[col] = pd.to_numeric(df[col], errors='coerce')
                df[col] = df[col].astype('Int64')  # Capital 'I' for nullable integer
            elif col in ['doctor_id', 'doctor_type_id', 'service_type_id']:
                df[col] = df[col].astype('category')
            elif col == 'distance_to_appointment_km':
                df[col] = df[col].astype('float64')
        except (ValueError, TypeError) as e:
            print(f"Error converting '{col}': {e}")
    return df


def fill_missing_text_fields(df: pd.DataFrame, columns: list) -> pd.DataFrame:
    """
    Fill missing values in text columns with the mode.
    """
    for col in columns:
        df[col] = df[col].fillna(df[col].mode()[0])
    return df


def standardize_text(df: pd.DataFrame, columns: list) -> pd.DataFrame:
    """
    Convert text columns to lowercase and strip whitespace.
    """
    for col in columns:
        df[col] = df[col].str.lower().str.strip()
    return df


def handle_patient_birth_year(df: pd.DataFrame) -> pd.DataFrame:
    """
    For invalid birth years converts them to None.
    
    Args:
        df (pd.DataFrame): Input DataFrame containing patient data
        
    Returns:
        pd.DataFrame: Processed DataFrame with birth years converted to None where appropriate
    """
    try:
        # Create a copy to avoid SettingWithCopyWarning
        df = df.copy()
        
        # Convert NULL values to None
        df['patient_birth_year'] = df['patient_birth_year'].where(pd.notna(df['patient_birth_year']), None)
        
        # Convert invalid birth years to None
        df['patient_birth_year'] = df['patient_birth_year'].apply(
            lambda x: None if x is None or x < MIN_VALID_BIRTH_YEAR else x
        )
        
        return df
        
    except Exception as e:
        print(f"Error in handle_patient_birth_year: {e}")
        return df

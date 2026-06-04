import pandas as pd
from .utils import categorize_time, is_children_holiday, calculate_age


def create_days_until_appointment(df: pd.DataFrame) -> pd.DataFrame:
    df['days_until_appointment'] = (
        df['appointment_date'] - df['appointment_date_registration']
    ).dt.days
    return df


def add_time_period(df: pd.DataFrame) -> pd.DataFrame:
    df['appointment_time_period'] = (
        df['appointment_time_hour'].apply(categorize_time)
    ).astype('category')
    return df


def add_day_of_week(df: pd.DataFrame) -> pd.DataFrame:
    df['appointment_day_of_week'] = (
        df['appointment_date'].dt.dayofweek
    ).astype('category')
    return df


def add_repeated_patient_features(df: pd.DataFrame) -> pd.DataFrame:
    df['is_repeated_patient'] = (
        df.groupby(['patient_id', 'doctor_id']).cumcount() > 0
    ).astype(int)
    df['past_appointments_count'] = df.groupby('patient_id').cumcount()
    df['past_missed_appointments'] = (
        df.groupby('patient_id')['appointment_attended']
          .transform(lambda x: x.eq(0).shift().fillna(0).astype(int).cumsum())
    )
    return df


def add_children_holiday(df: pd.DataFrame) -> pd.DataFrame:
    df['children_holiday'] = (
        df['appointment_date'].apply(is_children_holiday)
    ).astype('bool')
    return df


def calculate_patient_age(df: pd.DataFrame) -> pd.DataFrame:
    df['patient_age'] = df['patient_birth_year'].apply(calculate_age)
    median_age = df['patient_age'].median()
    df['patient_age'] = df['patient_age'].fillna(median_age).astype('int64')
    return df

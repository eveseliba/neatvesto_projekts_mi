import pandas as pd
import datetime

def categorize_time(hour: int) -> str:
    """
    Categorize an hour into a time period.
    """
    if 6 <= hour <= 11:
        return 'morning'
    elif 12 <= hour <= 17:
        return 'afternoon'
    elif 18 <= hour <= 23:
        return 'evening'
    else:
        return 'night'


def is_children_holiday(date: pd.Timestamp) -> bool:
    """
    Check if a date falls within common school holiday periods.
    """
    month = date.month
    day = date.day
    # Spring break: March 10-20
    if month == 3 and 10 <= day <= 20:
        return True
    # Summer vacation: June 1 - August 31
    if 6 <= month <= 8:
        return True
    # Fall break: October 20-31
    if month == 10 and day >= 20:
        return True
    # Winter/Christmas break: December 23 - January 7
    if (month == 12 and day >= 23) or (month == 1 and day <= 7):
        return True
    return False


def calculate_age(birth_year) -> int:
    """
    Calculate age based on birth year, handling nulls and future years.
    """
    current_year = datetime.datetime.now().year
    if pd.isna(birth_year):
        return None
    if birth_year > current_year:
        return 0
    return current_year - int(birth_year)

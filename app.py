import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# Now your decorators and rest of the code can safely use st.cache_data
@st.cache_data(ttl=60, show_spinner=False)
def cached_twelve_data(sym, interval, apikey):
    ...

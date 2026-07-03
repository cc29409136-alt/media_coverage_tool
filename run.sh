#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
streamlit run app.py --server.address localhost

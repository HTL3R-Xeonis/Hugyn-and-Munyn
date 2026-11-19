FROM python:3.15-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gns3_api_app.py app.py

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the source code from src directory
COPY src/ /app/src/

EXPOSE 3000

# Update the path to the main script
CMD ["python", "src/r53ns-monitor.py"]

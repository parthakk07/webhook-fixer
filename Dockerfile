FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FIXER_PUBLIC_DEMO=1 PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "streamlit run app.py --server.port ${PORT} --server.address 0.0.0.0 --server.headless true --server.fileWatcherType none --server.runOnSave false"]

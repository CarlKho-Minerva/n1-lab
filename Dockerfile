FROM python:3.12-slim
WORKDIR /app
COPY app.py .
COPY fonts/ fonts/
COPY sample_data/ sample_data/
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-u", "app.py"]

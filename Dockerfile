FROM python:3.10

COPY . /app
WORKDIR /app

EXPOSE 8000

RUN pip install pipenv
RUN pipenv install --system --deploy

CMD ["pipenv", "run", "prod"]

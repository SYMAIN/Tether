@echo off
cd X:\Dev\Tether
docker-compose up --build -d
echo Tether is running. Type "docker-compose logs -f tether" to view logs.
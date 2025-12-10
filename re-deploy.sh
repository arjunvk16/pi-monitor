docker compose down
sleep 5
docker rmi arjunvk/ai-agent:1.0.0
docker build -f Dockerfile.ai -t arjunvk/ai-agent:1.0.0 . --no-cache
docker compose up -d

#!/usr/bin/env bash
# Сборка и публикация образа в реестр.
#
#   docker login
#   tools/publish-image.sh <namespace> [тег]
#
# Собирается сразу под amd64 и arm64: жюри может открыть проект на ноутбуке с Apple Silicon,
# и образ только под amd64 там либо не запустится, либо пойдёт через эмуляцию.
set -euo pipefail

NS="${1:?укажите namespace реестра, например omazda}"
TAG="${2:-$(git -C "$(dirname "$0")/.." describe --tags --always --dirty 2>/dev/null || echo latest)}"
IMAGE="${NS}/constellation"
cd "$(dirname "$0")/.."

echo "Собираю ${IMAGE}:${TAG} и ${IMAGE}:latest под linux/amd64 и linux/arm64"

# Сборка под несколько архитектур требует отдельного билдера: драйвер docker по умолчанию
# умеет только родную платформу хоста.
docker buildx inspect constellation-builder >/dev/null 2>&1 \
  || docker buildx create --name constellation-builder --driver docker-container --use
docker buildx use constellation-builder

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --tag "${IMAGE}:${TAG}" \
  --tag "${IMAGE}:latest" \
  --push \
  .

echo
echo "Готово. Проверить запуск из реестра:"
echo "  CONSTELLATION_IMAGE=${IMAGE}:latest docker compose -f docker-compose.hub.yml up -d"

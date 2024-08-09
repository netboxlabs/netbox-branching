ifneq ($(shell docker compose version 2>/dev/null),)
  DOCKER_COMPOSE := docker compose
else
  DOCKER_COMPOSE := docker-compose
endif

.PHONY: docker-compose-up
docker-compose-up:
	@$(DOCKER_COMPOSE) -f docker/docker-compose.yaml up -d

.PHONY: docker-compose-down
docker-compose-down:
	@$(DOCKER_COMPOSE) -f docker/docker-compose.yaml down

.PHONY: docker-compose-test
docker-compose-test:
	@$(DOCKER_COMPOSE) -f docker/docker-compose.yaml run -u root --rm netbox ./manage.py test --keepdb netbox_branching
	@$(MAKE) docker-compose-down

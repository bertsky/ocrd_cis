PY ?= python3
PIP ?= pip3
V ?= > /dev/null 2>&1
PKG = ocrd_cis

install:
	${PIP} install --upgrade pip .
install-devel:
	${PIP} install --upgrade pip -e .
uninstall:
	${PIP} uninstall ${PKG}

docker-build: Dockerfile
	docker build \
	--build-arg VCS_REF=$$(git rev-parse --short HEAD) \
	--build-arg BUILD_DATE=$$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
	-t flobar/ocrd_cis:latest .
docker-push: docker-build
	docker push flobar/ocrd_cis:latest

TEST_SCRIPTS=$(sort $(wildcard tests/run_*.bash))
.PHONY: $(TEST_SCRIPTS)
$(TEST_SCRIPTS):
	bash $@ $V
test: $(TEST_SCRIPTS)
	echo $^
.PHONY: install test

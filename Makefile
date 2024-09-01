PY ?= python3
PIP ?= pip3
V ?= > /dev/null 2>&1
PKG = ocrd_cis
TAG = flobar/ocrd_cis

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
	-t $(TAG):latest .
docker-push: docker-build
	docker push $(TAG):latest

TEST_SCRIPTS=$(sort $(filter-out tests/run_training_test.bash, $(wildcard tests/run_*.bash)))
.PHONY: $(TEST_SCRIPTS)
$(TEST_SCRIPTS):
	OCRD_MAX_PARALLEL_PAGES=1 /usr/bin/time -o test_serially.log -a -f "$@: %Uuser %Ssystem %Eelapsed %PCPU (%Mmax)k" bash $@ $V
	OCRD_MAX_PARALLEL_PAGES=4 /usr/bin/time -o test_parallel.log -a -f "$@: %Uuser %Ssystem %Eelapsed %PCPU (%Mmax)k" bash $@ $V

test: export OCRD_OVERRIDE_LOGLEVEL=DEBUG
test: export OCRD_MISSING_OUTPUT=ABORT
test: export OCRD_MAX_MISSING_OUTPUTS=-1
test: $(TEST_SCRIPTS)
	@echo =====single-threaded test results=====
	@cat test_serially.log
	@echo =====4-page-parallel test results=====
	@cat test_parallel.log
	@$(RM) test_serially.log test_parallel.log

.PHONY: install install-devel uninstall test docker-build docker-push

# Docker 없이 sam build를 수행하기 위한 커스텀 빌드 (template.yaml의 BuildMethod: makefile).
# 로컬 Python 인터프리터 버전과 무관하게, pip의 크로스 플랫폼 다운로드 옵션으로 Lambda
# 런타임(python3.14, linux x86_64)에 맞는 wheel을 직접 내려받아 패키징합니다.
PY_VERSION := 3.14
PY_ABI := cp314
PLATFORM := manylinux2014_x86_64

build-RefTableProcessorFunction:
	cp app.py $(ARTIFACTS_DIR)/
	python3 -m pip install \
		--platform $(PLATFORM) \
		--implementation cp \
		--python-version $(PY_VERSION) \
		--abi $(PY_ABI) \
		--only-binary=:all: \
		--target $(ARTIFACTS_DIR) \
		-r requirements.txt

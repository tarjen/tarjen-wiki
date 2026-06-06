.PHONY: test test-py test-js build clean

# 跑所有本地测试
test: test-py test-js

# Python 单元测试（std-lib only，无需 pip install）
test-py:
	python3 -m unittest discover tests/ -v

# JS 单元测试（用 node 自带 test runner，无需 npm install）
test-js:
	node --test tests/js/*.test.js

# 验证 mkdocs 能成功构建（优先用 .venv，没有就 fallback 系统 python）
build:
	@if [ -x .venv/bin/mkdocs ]; then .venv/bin/mkdocs build --strict; \
	elif command -v mkdocs >/dev/null 2>&1; then mkdocs build --strict; \
	else echo "❌ mkdocs not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; \
	fi

clean:
	rm -rf site/

.PHONY: test test-py test-js build clean

# 跑所有本地测试
test: test-py test-js

# Python 单元测试（std-lib only，无需 pip install）
test-py:
	python3 -m unittest discover tests/ -v

# JS 单元测试（用 node 自带 test runner，无需 npm install）
test-js:
	node --test tests/js/

# 验证 mkdocs 能成功构建（注意：要先有 .venv 装过 mkdocs-material）
build:
	python3 -m mkdocs build --strict

clean:
	rm -rf site/

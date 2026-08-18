# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

project = Path(SPECPATH).parent.parent
app = Path(SPECPATH)

a = Analysis(
    [str(app / 'server.py')],
    pathex=[str(project / 'tools')],
    binaries=[],
    datas=[
        (str(app / 'public'), 'public'),
        (str(project / 'data' / 'workflows' / 'stage8'), 'data/workflows/stage8'),
        (str(project / 'specs'), 'specs'),
        (str(project / 'sql'), 'sql'),
        (str(project / 'sql' / '008_consumer_fund_manager_workbench.sql'), 'sql'),
        (str(project / 'sql' / '009_remove_human_review_gate.sql'), 'sql'),
        (str(project / 'sql' / '001_consumer_research_warehouse.sql'), 'sql'),
        (str(project / 'sql' / '002_consumer_research_model_engine.sql'), 'sql'),
        (str(project / 'sql' / '003_consumer_research_workflow_engine.sql'), 'sql'),
        (str(project / 'sql' / '007_consumer_research_task_library.sql'), 'sql'),
        (str(project / 'specs' / 'products' / 'consumer-research-task-library.v1.json'), 'specs/products'),
        (str(project / 'specs' / 'workflows' / 'consumer-research-workflow.v1.json'), 'specs/workflows'),
        (str(project / 'specs' / 'models' / 'consumer-research-model-engine.v1.json'), 'specs/models'),
        (str(project / 'tools' / 'consumer_knowledge_store.py'), 'tools'),
        (str(project / 'tools' / 'consumer_data_production.py'), 'tools'),
        (str(project / 'tools' / 'consumer_model_engine.py'), 'tools'),
        (str(project / 'tools' / 'consumer_workflow_engine.py'), 'tools'),
        (str(project / 'tools' / 'consumer_task_library.py'), 'tools'),
        (str(project / 'tools' / 'consumer_realtime_monitor.py'), 'tools'),
        (str(project / 'tools' / 'full_consumer_coverage.py'), 'tools'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='consumer-research-workbench',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

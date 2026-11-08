"""Fail-closed static policy for the approved Max Smart migrations.

This module reads migration source with :mod:`ast`. It never imports a
migration module. The SHA-256 values below identify the exact approved SQL
string values while keeping a second copy of the physical SQL out of source.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


class MigrationPolicyError(RuntimeError):
    """The migration package does not match its closed approved inventory."""


@dataclass(frozen=True)
class MigrationInventory:
    """Validated physical SQL recovered without importing migrations."""

    forward_sql: dict[str, tuple[str, ...]]
    reverse_sql: dict[str, tuple[str, ...]]
    seed_sql: dict[str, str]

    @property
    def object_sql(self):
        statements = (
            self.forward_sql["0001_v1_schema.py"]
            + self.forward_sql["0002_v1_integrity_triggers.py"]
            + self.forward_sql["0003_v1_seeds_and_retention.py"]
        )
        result = {}
        for statement in statements:
            match = re.match(
                r"CREATE (?:UNIQUE )?(TABLE|INDEX|TRIGGER) ([a-z0-9_]+)\b",
                statement,
            )
            if match is None:
                raise MigrationPolicyError(
                    "An approved physical statement has an invalid object header."
                )
            result[match.group(2)] = statement
        return result


_MIGRATION_DIRECTORY = Path(__file__).resolve().parent / "migrations"
_FILES = (
    "__init__.py",
    "0001_v1_schema.py",
    "0002_v1_integrity_triggers.py",
    "0003_v1_seeds_and_retention.py",
)
_SOURCE_DIGESTS = {
    "__init__.py": (
        "0a9f174af5545620ebd43e7c568e367d4c265f0f052561769b3ba0c657956e0e"
    ),
    "0001_v1_schema.py": (
        "e3d56a3b04417bfca13c7c0669be4c5ab5dd6d6d668d3773792208c662996d56"
    ),
    "0002_v1_integrity_triggers.py": (
        "90126f1f2ea392082664ab9e78927601044cfa5a301d72a233a8e889128ac6ce"
    ),
    "0003_v1_seeds_and_retention.py": (
        "36c9434d3c6c42276166791f54b5e9e3aa0d877e6a673ed3a69e62e6e7631627"
    ),
}

_EXPECTED_IMPORTS = {
    "__init__.py": frozenset(),
    "0001_v1_schema.py": frozenset(
        {
            ("from", "django.conf", 0, (("settings", None),)),
            (
                "from",
                "django.core.validators",
                0,
                (
                    ("MaxValueValidator", None),
                    ("MinValueValidator", None),
                ),
            ),
            (
                "from",
                "django.db",
                0,
                (("migrations", None), ("models", None)),
            ),
            (
                "from",
                "django.db.migrations.exceptions",
                0,
                (("IrreversibleError", None),),
            ),
            ("import", (("django.db.models.deletion", None),)),
            ("import", (("max_smart_db.fields", None),)),
        }
    ),
    "0002_v1_integrity_triggers.py": frozenset(
        {
            (
                "from",
                "django.db",
                0,
                (("migrations", None),),
            ),
            (
                "from",
                "django.db.migrations.exceptions",
                0,
                (("IrreversibleError", None),),
            ),
        }
    ),
    "0003_v1_seeds_and_retention.py": frozenset(
        {
            (
                "from",
                "django.db",
                0,
                (("migrations", None),),
            ),
            (
                "from",
                "django.db.migrations.exceptions",
                0,
                (("IrreversibleError", None),),
            ),
        }
    ),
}

_FORWARD_DIGESTS = {
    "0001_v1_schema.py": (
        "d94dcbde0f4b5f2b26625c1a51f656f75c82c2979072e261fce1683c5013f074",
        "1bb976baea5036b76e70be6834795cda409f605d65bc667fa68813e041d6858e",
        "3e1e9c6bef2390764f593edfcf55507c82dbab06f1535bb8926e1fc68321243c",
        "22fbd3dc41bed3158a208904d7cb42536c6f3a031a9257ce2e80d4dea3b5a484",
        "6bc861d09436027ee1d02b687ef8683fda077db8cd01555acca71af614a3d6c7",
        "c680889172eb3c5cf122ba480d311489ae6bbb23ff571dd7668214d4d3e923b9",
        "f52a73bee6e7d2231737334da65c4cb7a9088662c87c860c56b45b4f0edc2e7e",
        "6280e19f548bee73e9d5127f3346f428507cf78926c8392faec0436fe894371b",
        "f0550a5233b4c236cf06395f0a0d55644b92e98714f8b8621d373ad0e2b99889",
        "d1f09e777dd88641b8ef2750cd2d748b824e5202d23acec152212bc5069aba47",
        "ab31fdd3dd2d0b17b8b171629d6eeedce4a4cdf4ebf5228a342e57de05452e86",
        "470ea40a921e876068995c8388b5672a39443db24ed3f5b870595e818f3f9bdd",
        "104c0fc18243e88572aa4923845c0212089cac5743265be3216241b16b0d3e69",
        "3489d58e854c1f0a177856d6c035645915d70a275c2cd4600149867081eb244e",
        "5e93274f17c7434bfcd24d511866b1ebfcf35b0ecae13e83a7fa8c89f549cbca",
        "fc500046344a7fc5dd1027ce21e2be1360a68d65533ce9c20742e9f36b278e2b",
        "7eedaea70be607ea4831546f0d9de2815c0d29171d306b01d3f9fd7a66e56400",
        "ed77d062b911d751161052e5c73d37db93fce9763d28c4ece9e5b27ddf33871b",
        "483866557344c74fe878803f4b21ec4ef5c715c8f4d198b4a91775ed4d500b03",
        "f48652948603862a188a0b27a6835b9ef5d8f1a710710da2f300470812dba8ed",
        "288c4efd47545821c9bda9460994365346269ab06cceec3c05e05b34e9c06505",
        "8b8945516e25f591101ce2fa2d1afb56ec6aacd05b8e77e1ebb8053ea5dc73e8",
        "3381a5137961f6f0af6e77d2bfe6ec0ee4a9d3c2d582a9feb9377929b036e2f1",
    ),
    "0002_v1_integrity_triggers.py": (
        "f57c2f139adefee0437cdc37e594669d0668fe32d429c29e6585242ae8222670",
        "39e017cfae3dc4c12b44657c22f5503b11d3bb5e201b2c9b25bba410ad4e0ae1",
        "e1bf29d65138df59e93fc701b7a951b886b36f179a5d4deba33b4417ac8f8190",
        "f8c69352d146d1e01fab38204612f7c85815382b7c161758427f67cfa4ab14cf",
        "1dbac8cd84ea449574b2981e3291fd414eb185820caa76e355ea30fae15a2c3b",
        "05f1001d5350f7fd2762bcaf90cf99900d1823b0e827a7dba584e81ddce28fdf",
        "097784422470cb56ccba73a234fcff803a8d61070e9c0161b9707eaee3e5e42b",
        "b3cf9dd0f50f98ce9d5fd05e78f8e0324871dc50e3879751cf3bdf21a52dab54",
        "050214f941322b929bac28eee01740dc9c57e864e1298533aba9adf3c4a75296",
        "3f6c1ab7a4c8b3a8956283469375fa3a1c51f8060b79abb13b301893faf0181d",
        "f2fe47e4e9501d699a503c458e81375d9158314693699f1d10ebcd8986577ab6",
    ),
    "0003_v1_seeds_and_retention.py": (
        "c859f744457b3479f6573cf25abfa420c2169ee326b9fb975b12073cbdb0a624",
        "56ae0adc1ce8370b11bb7e253f0e5587c6126700262d55ccaf3eae224b71e2b5",
        "53f16e073025c78b8cc44b5650b3a10ef6c6de927ed82a2020e19d93b04c6dd8",
        "774a5057fff1fb2eee885612fbe6988f73448696174870788cb9b382c795946c",
        "421c188569658c4bdd0af6395fd9f6dfe27cab6295a33202b9cfaece661a658d",
        "7f79c190817972d5fa3820df2e37e4ffc6a7ae051bc424c8f27d21f73c6d1dcc",
    ),
}

_REVERSE_DIGESTS = {
    "0001_v1_schema.py": (
        "0186f32dac200f5bded4b184a098ff7b2b6e2970501433148b36aecb393c6d75",
        "8b61f429493b539dd61cd18b21d1fc60815b9d714c8a816ba39c60fa4e40d135",
        "19156db2ef7dbaf1e8c5f0cb29fa1c0fe8644a2316ba492324b848d213f86b4e",
        "a0064f6f11152596afa7c85c48a20e5d1c6da150efbb7b1637872e3121d42d4c",
        "e5135526c8b8de1927ca591afbc088b251ddfe29892548e5a5de7a5872be77e8",
        "d8c1b728057f15a48045b44213e3136ebf7b1d17d310febaf25ad848d209ca54",
        "b13f8db6df710680a8230a69e568a7779a8b90e9010ddcfdd56faf1ef52f47d1",
        "a15bdae5e09794c8be9e08e18b7d532b3358cecd5a761a5d62b7c9b1dabedc3f",
        "d40c04e1b34f480e17350ce037fde8956cedfd4d40119ee10d5eb323d22fc31f",
        "211c7b657521c323dcc4f790b0962c3776e4f3c00ecd5412c55d7673726390c5",
        "b5bf19e2249ff3a680553c1718e79f35a646a24d0578fba05dde65598e9bfd45",
        "70ead8b9ef3c19e4e8fbd8be2a3e62000575fbfdf1a7ebd1676f310d5c0c2bcf",
        "6dd7a923d486501ff4ac1ec343fa5bf26de0d18d85e9a5c5b1701a17d3c46882",
        "8097a2015139bc43920272ef4e2f487b65ee3f1388d4f8d00bd35ede32a738fa",
        "cc395434cee523549966eeaed2f798fb2c7a37db8823a2bd67ef90a2b288bf3a",
        "497e60904b446867a8aa8a1fa6f3bda082473d86e18feee45dfddf77aedf197b",
        "3d4a680cbc4190be3c967d9e07b9bec8f5deaff2034284059fc827ae546fd71c",
        "f5a7f40de8ac1e630db256d9a23eebdfa5326606343e0c49a7421ffebfa5ba2c",
        "e402c3ecf3801706d8b78e182dac44b093cede6c16deeb699de432c88919391b",
        "a5949fe75a02abe1143da672adf205ab365c392898354f0891f0aef01cf46990",
        "2831e445b26f81b2282c63ba9611872d87203d515d961232d2788bbdd53477a7",
        "8f9193cf4b4042a296886cbba87c98395bc129777cf0f5a771dbb74e76038269",
        "e0c878460e7f820b79c42b003a6dde11fddd7835bbb0d2bbfa626c95f40d4f2c",
    ),
    "0002_v1_integrity_triggers.py": (
        "9039fe80bfb8b9c97792ac85209aec4077d3c4ecba98a73560682296219ec8d2",
        "0e3d821fb4c1448ac2d37bb1a3652c4080f0e81cf93245c97ef45da79647e247",
        "8b6626ad29d4c87094aa1916fd56530089f013d9c721bcb1489df72042f3612f",
        "9fbe73f5a3b1d9c93d3e0c5cd1a4ea750e3f6e97746a8bbd7e83c50c4bfb85db",
        "33fa6a4f63a7b44d82513b00159c18c626c7d728da99fd359821bcc235528833",
        "a1a432e242e0a84a343bdf4b8dc362f6e292625bb2b46233f59dc14a0302d508",
        "7585dd34739fcbb37d8c5197eb66d30f1d0f8d37d5c639d3d2a98a472479a501",
        "a0bac9bdc9cbac7cc09dced557bb387ed7bb54d8ffed7ee9fa03942cad9e9be2",
        "0b93a23de40c2c4053ebfaaff1f19a7af4f669ec340a7f695648158ec93b4108",
        "b2f172c44f6557f432ea34f988ee8f740f9638c5ae5b494d4c0897e535916029",
        "f998cef974cda11410e7a8d99a2e8b2ee92c20c40b76d0cd86624cfe8035e832",
    ),
    "0003_v1_seeds_and_retention.py": (
        "a5c64c32b3017267f292f01ac396913e515538dea1f364745a6a8588d534f37b",
        "89ef7f53ab71ff993416cf6e53d5c4ae531cbc34db854f41dfa2fc91cf723956",
        "2c08c41ce1f1e57c4c9079159f3836bf8cd6c9a61c43f037d62973b53cb7f89c",
        "35616abfb3ad48c65a74d595c0e2a9a194d40d2c4b6fa72ce122ef991f60eed6",
        "267d748c2e69de47813901447c16825e7a29b05dcd3f1418059b7ab5091a6561",
        "a4788ae5b589b6ef1b12143bfcad20d07752ef58062b8c98174f31076fdd62f5",
    ),
}

_SEED_DIGESTS = {
    "ROLE_INSERT_SQL": "82d13dfcb1fbb31a9e7f56e90c4114b2ac6705f5593b6549bbf7e5c496918eeb",
    "ROLE_DELETE_SQL": "56089e971083cf2e35569d147136a72443df33d277e654a3acc11b483fcc6367",
    "SLOT_INSERT_SQL": "f0529284d93b000c10487d42bb7732e31e2ef8b85821fe016443df0041d40d6a",
    "SLOT_DELETE_SQL": "b135ece7c67f2fa688584e7310f47992028801681abd39b8faa94546d4b1d105",
}

_MIGRATION_0001_COUNT_SQL = (
    "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' "
    "AND name = '0001_v1_schema';"
)
_MIGRATION_0002_COUNT_SQL = (
    "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' "
    "AND name = '0002_v1_integrity_triggers';"
)
_MIGRATION_0003_COUNT_SQL = (
    "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' "
    "AND name = '0003_v1_seeds_and_retention';"
)
_UNEXPECTED_MIGRATION_COUNT_SQL = (
    "SELECT COUNT(*) FROM django_migrations WHERE app = 'max_smart_db' "
    "AND (name IS NULL OR name NOT IN ('0001_v1_schema', "
    "'0002_v1_integrity_triggers', '0003_v1_seeds_and_retention'));"
)
_GUARD_SQL = {
    "MIGRATION_0001_COUNT_SQL": _MIGRATION_0001_COUNT_SQL,
    "MIGRATION_0002_COUNT_SQL": _MIGRATION_0002_COUNT_SQL,
    "MIGRATION_0003_COUNT_SQL": _MIGRATION_0003_COUNT_SQL,
    "UNEXPECTED_MIGRATION_COUNT_SQL": _UNEXPECTED_MIGRATION_COUNT_SQL,
    "AI_EMPTY_SQL": "SELECT EXISTS (SELECT 1 FROM authorized_identity LIMIT 1);",
    "ROLE_EMPTY_SQL": "SELECT EXISTS (SELECT 1 FROM role LIMIT 1);",
    "CONVERSATION_EMPTY_SQL": (
        "SELECT EXISTS (SELECT 1 FROM conversation LIMIT 1);"
    ),
    "OPERATION_EMPTY_SQL": "SELECT EXISTS (SELECT 1 FROM operation LIMIT 1);",
    "MESSAGE_EMPTY_SQL": "SELECT EXISTS (SELECT 1 FROM message LIMIT 1);",
    "IDEMPOTENCY_EMPTY_SQL": (
        "SELECT EXISTS (SELECT 1 FROM idempotency_key LIMIT 1);"
    ),
    "SLOT_EMPTY_SQL": (
        "SELECT EXISTS (SELECT 1 FROM global_model_slot LIMIT 1);"
    ),
    "ROLE_BASELINE_SQL": (
        "SELECT slug, display_name, active FROM role ORDER BY slug;"
    ),
    "SLOT_BASELINE_SQL": (
        "SELECT id, operation_id, runner_id, reservation_expires_at_us, "
        "lease_expires_at_us, acquired_at_us FROM global_model_slot ORDER BY id;"
    ),
}

_GUARD_CONSTANTS = {
    "0001_v1_schema.py": frozenset(
        {
            "MIGRATION_0001_COUNT_SQL",
            "MIGRATION_0002_COUNT_SQL",
            "MIGRATION_0003_COUNT_SQL",
            "UNEXPECTED_MIGRATION_COUNT_SQL",
            "AI_EMPTY_SQL",
            "ROLE_EMPTY_SQL",
            "CONVERSATION_EMPTY_SQL",
            "OPERATION_EMPTY_SQL",
            "MESSAGE_EMPTY_SQL",
            "IDEMPOTENCY_EMPTY_SQL",
            "SLOT_EMPTY_SQL",
        }
    ),
    "0002_v1_integrity_triggers.py": frozenset(
        {
            "MIGRATION_0001_COUNT_SQL",
            "MIGRATION_0002_COUNT_SQL",
            "MIGRATION_0003_COUNT_SQL",
            "UNEXPECTED_MIGRATION_COUNT_SQL",
            "AI_EMPTY_SQL",
            "ROLE_EMPTY_SQL",
            "CONVERSATION_EMPTY_SQL",
            "OPERATION_EMPTY_SQL",
            "MESSAGE_EMPTY_SQL",
            "IDEMPOTENCY_EMPTY_SQL",
            "SLOT_EMPTY_SQL",
        }
    ),
    "0003_v1_seeds_and_retention.py": frozenset(
        {
            "MIGRATION_0001_COUNT_SQL",
            "MIGRATION_0002_COUNT_SQL",
            "MIGRATION_0003_COUNT_SQL",
            "UNEXPECTED_MIGRATION_COUNT_SQL",
            "AI_EMPTY_SQL",
            "CONVERSATION_EMPTY_SQL",
            "OPERATION_EMPTY_SQL",
            "MESSAGE_EMPTY_SQL",
            "IDEMPOTENCY_EMPTY_SQL",
            "ROLE_BASELINE_SQL",
            "SLOT_BASELINE_SQL",
        }
    ),
}

_EXECUTE_INVENTORY = {
    "0001_v1_schema.py": {
        "guard_0001": (
            "MIGRATION_0001_COUNT_SQL",
            "MIGRATION_0002_COUNT_SQL",
            "MIGRATION_0003_COUNT_SQL",
            "UNEXPECTED_MIGRATION_COUNT_SQL",
            "AI_EMPTY_SQL",
            "ROLE_EMPTY_SQL",
            "CONVERSATION_EMPTY_SQL",
            "OPERATION_EMPTY_SQL",
            "MESSAGE_EMPTY_SQL",
            "IDEMPOTENCY_EMPTY_SQL",
            "SLOT_EMPTY_SQL",
        ),
    },
    "0002_v1_integrity_triggers.py": {
        "guard_0002": (
            "MIGRATION_0001_COUNT_SQL",
            "MIGRATION_0002_COUNT_SQL",
            "MIGRATION_0003_COUNT_SQL",
            "UNEXPECTED_MIGRATION_COUNT_SQL",
            "AI_EMPTY_SQL",
            "ROLE_EMPTY_SQL",
            "CONVERSATION_EMPTY_SQL",
            "OPERATION_EMPTY_SQL",
            "MESSAGE_EMPTY_SQL",
            "IDEMPOTENCY_EMPTY_SQL",
            "SLOT_EMPTY_SQL",
        ),
    },
    "0003_v1_seeds_and_retention.py": {
        "seed_role_forward": ("ROLE_INSERT_SQL",),
        "seed_role_reverse": ("ROLE_DELETE_SQL",),
        "seed_slot_forward": ("SLOT_INSERT_SQL",),
        "seed_slot_reverse": ("SLOT_DELETE_SQL",),
        "guard_0003": (
            "MIGRATION_0001_COUNT_SQL",
            "MIGRATION_0002_COUNT_SQL",
            "MIGRATION_0003_COUNT_SQL",
            "UNEXPECTED_MIGRATION_COUNT_SQL",
            "AI_EMPTY_SQL",
            "CONVERSATION_EMPTY_SQL",
            "OPERATION_EMPTY_SQL",
            "MESSAGE_EMPTY_SQL",
            "IDEMPOTENCY_EMPTY_SQL",
            "ROLE_BASELINE_SQL",
            "SLOT_BASELINE_SQL",
        ),
    },
}

_ALLOWED_CALLS = frozenset(
    {
        "<literal>.join",
        "IrreversibleError",
        "MaxValueValidator",
        "MinValueValidator",
        "_exact_nonnegative_scalar",
        "_raise_guard_failures",
        "_record_emptiness",
        "_record_named_count",
        "any",
        "cursor.execute",
        "cursor.fetchall",
        "cursor.fetchone",
        "isinstance",
        "len",
        "max_smart_db.fields.SQLiteInteger64Field",
        "messages.append",
        "migrations.CreateModel",
        "migrations.RunPython",
        "migrations.RunSQL",
        "migrations.SeparateDatabaseAndState",
        "models.ForeignKey",
        "models.Index",
        "models.IntegerField",
        "models.Q",
        "models.TextField",
        "models.UUIDField",
        "models.UniqueConstraint",
        "schema_editor.connection.cursor",
        "type",
    }
)
_FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "objects",
        "raw",
        "extra",
        "executemany",
        "executescript",
    }
)
_FORBIDDEN_NAMES = frozenset(
    {
        "MigrationRecorder",
        "eval",
        "exec",
    }
)
_SQL_PREFIX = re.compile(
    r"^\s*(?:SELECT\b|INSERT\s+INTO\b|UPDATE\b|DELETE\s+FROM\b|"
    r"REPLACE\b|CREATE\s+(?:TABLE|INDEX|UNIQUE|TRIGGER)\b|"
    r"DROP\s+(?:TABLE|INDEX|TRIGGER)\b|ALTER\s+TABLE\b|PRAGMA\b|"
    r"BEGIN\b|COMMIT\b|ROLLBACK\b)",
    re.IGNORECASE,
)


def _fail(filename, node, message):
    line = getattr(node, "lineno", 0)
    raise MigrationPolicyError(f"{filename}:{line}: {message}")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _import_signature(node):
    if isinstance(node, ast.Import):
        return (
            "import",
            tuple((item.name, item.asname) for item in node.names),
        )
    return (
        "from",
        node.module,
        node.level,
        tuple((item.name, item.asname) for item in node.names),
    )


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    if isinstance(node, ast.Constant):
        return "<literal>"
    return None


def _top_level_string_assignments(tree):
    result = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            result[node.targets[0].id] = node.value.value
    return result


def _one_literal_list(filename, node, label):
    if (
        not isinstance(node, ast.List)
        or len(node.elts) != 1
        or not isinstance(node.elts[0], ast.Constant)
        or not isinstance(node.elts[0].value, str)
    ):
        _fail(filename, node, f"{label} must be a one-element literal list.")
    return node.elts[0].value


def _validate_imports(filename, tree):
    imports = [
        _import_signature(node)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    if len(imports) != len(set(imports)):
        raise MigrationPolicyError(f"{filename}: duplicate import.")
    if frozenset(imports) != _EXPECTED_IMPORTS[filename]:
        raise MigrationPolicyError(f"{filename}: import inventory is not approved.")


def _validate_calls(filename, tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            _fail(filename, node, f"{node.id} is prohibited.")
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _FORBIDDEN_ATTRIBUTES
        ):
            _fail(filename, node, f".{node.attr} is prohibited.")
        if isinstance(
            node,
            (
                ast.AsyncFunctionDef,
                ast.Await,
                ast.Lambda,
                ast.NamedExpr,
                ast.Yield,
                ast.YieldFrom,
            ),
        ):
            _fail(filename, node, "unapproved dynamic syntax.")
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name == "<literal>":
            name = "<literal>.join"
        if name not in _ALLOWED_CALLS:
            _fail(filename, node, f"unresolved or unapproved call: {name!r}.")
        if name == "cursor.execute":
            if (
                len(node.args) != 1
                or node.keywords
                or not isinstance(node.args[0], ast.Name)
            ):
                _fail(
                    filename,
                    node,
                    "execute requires one approved local SQL constant.",
                )


def _validate_execute_inventory(filename, tree):
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    observed = {}
    for function_name, function in functions.items():
        names = []
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and _call_name(node.func) == "cursor.execute"
            ):
                names.append((node.lineno, node.args[0].id))
        if names:
            observed[function_name] = tuple(
                name for _, name in sorted(names)
            )
    if observed != _EXECUTE_INVENTORY.get(filename, {}):
        raise MigrationPolicyError(
            f"{filename}: execute-call inventory or order is not approved."
        )


def _validate_sql_constants(filename, assignments):
    expected_names = set(_GUARD_CONSTANTS.get(filename, ()))
    if filename == "0003_v1_seeds_and_retention.py":
        expected_names.update(_SEED_DIGESTS)
    observed_names = {
        name for name in assignments if name.endswith("_SQL")
    }
    if observed_names != expected_names:
        raise MigrationPolicyError(
            f"{filename}: SQL constant inventory is not approved."
        )
    for name in _GUARD_CONSTANTS.get(filename, ()):
        if assignments[name] != _GUARD_SQL[name]:
            raise MigrationPolicyError(
                f"{filename}: guard SQL {name} is not an exact match."
            )
    for name, expected_digest in _SEED_DIGESTS.items():
        if name in expected_names and _digest(assignments[name]) != expected_digest:
            raise MigrationPolicyError(
                f"{filename}: frozen seed SQL {name} is not an exact match."
            )


def _validate_runsql(filename, tree):
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _call_name(node.func) == "migrations.RunSQL"
        ):
            calls.append(node)
    calls.sort(key=lambda node: node.lineno)
    forward = []
    reverse = []
    for node in calls:
        if node.args:
            _fail(filename, node, "RunSQL positional arguments are prohibited.")
        keywords = {item.arg: item.value for item in node.keywords}
        if set(keywords) != {"sql", "reverse_sql"}:
            _fail(filename, node, "RunSQL keyword inventory is not approved.")
        forward.append(_one_literal_list(filename, keywords["sql"], "sql"))
        reverse.append(
            _one_literal_list(
                filename,
                keywords["reverse_sql"],
                "reverse_sql",
            )
        )
    forward_digests = tuple(_digest(value) for value in forward)
    reverse_digests = tuple(_digest(value) for value in reverse)
    if forward_digests != _FORWARD_DIGESTS.get(filename, ()):
        raise MigrationPolicyError(
            f"{filename}: frozen forward SQL or ordering differs."
        )
    if reverse_digests != _REVERSE_DIGESTS.get(filename, ()):
        raise MigrationPolicyError(
            f"{filename}: frozen reverse SQL or pairing differs."
        )
    return tuple(forward), tuple(reverse)


def _validate_no_other_sql(filename, tree, approved_values):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _SQL_PREFIX.match(node.value)
            and node.value not in approved_values
        ):
            _fail(filename, node, "unapproved SQL literal.")


def _scan_file(path):
    filename = path.name
    try:
        source_bytes = path.read_bytes()
        source = source_bytes.decode("utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as error:
        raise MigrationPolicyError(
            f"{filename}: migration source could not be parsed."
        ) from error
    _validate_imports(filename, tree)
    _validate_calls(filename, tree)
    assignments = _top_level_string_assignments(tree)
    _validate_sql_constants(filename, assignments)
    _validate_execute_inventory(filename, tree)
    forward, reverse = _validate_runsql(filename, tree)
    approved_values = set(forward) | set(reverse)
    approved_values.update(
        assignments[name]
        for name in assignments
        if name.endswith("_SQL")
    )
    _validate_no_other_sql(filename, tree, approved_values)
    if hashlib.sha256(source_bytes).hexdigest() != _SOURCE_DIGESTS[filename]:
        raise MigrationPolicyError(
            f"{filename}: approved migration source differs."
        )
    return forward, reverse, assignments


def scan_migration_package(package_path=None):
    """Validate every approved migration source before any migration import."""
    directory = (
        _MIGRATION_DIRECTORY
        if package_path is None
        else Path(package_path)
    )
    try:
        python_files = tuple(
            sorted(path.name for path in directory.glob("*.py"))
        )
    except OSError as error:
        raise MigrationPolicyError(
            "The migration directory could not be inspected."
        ) from error
    if python_files != tuple(sorted(_FILES)):
        raise MigrationPolicyError(
            "The migration module inventory is not approved."
        )

    forward_sql = {}
    reverse_sql = {}
    seed_sql = {}
    for filename in _FILES:
        forward, reverse, assignments = _scan_file(directory / filename)
        if filename != "__init__.py":
            forward_sql[filename] = forward
            reverse_sql[filename] = reverse
        if filename == "0003_v1_seeds_and_retention.py":
            seed_sql = {
                name: assignments[name]
                for name in _SEED_DIGESTS
            }
    if sum(len(values) for values in forward_sql.values()) != 40:
        raise MigrationPolicyError("The RunSQL inventory is incomplete.")
    if sum(len(values) for values in reverse_sql.values()) != 40:
        raise MigrationPolicyError("The reverse RunSQL inventory is incomplete.")
    if len(seed_sql) != 4:
        raise MigrationPolicyError("The frozen seed SQL inventory is incomplete.")
    return MigrationInventory(forward_sql, reverse_sql, seed_sql)

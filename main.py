import ast
import json
import pathlib
import re
import shutil
import sys
import tempfile
import subprocess
import asyncio
from pocketbase import PocketBase


async def authenticate_pb(pb: PocketBase, email, password):
	await pb.collection("_superusers").auth.with_password(
		username_or_email=email,
		password=password
	)

async def get_pb_schema(pb: PocketBase, include_system = False):
	return await pb.collections.get_full_list({"filter": f"system = {str(include_system).lower()}"})


def write_pb_schema_to_tmpfile(pb_schema_data):
	with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as pb_schema_file:
		pb_schema_file.write(json.dumps(pb_schema_data))
		pb_schema_file.flush()
		pb_schema_filepath = pb_schema_file.name
	return pb_schema_filepath

######


def generate_and_save_openapi_from_typescript_path(typescript_path):
	ts_path = pathlib.Path(typescript_path)

	# Create a temp folder
	tmp_dir = tempfile.TemporaryDirectory(delete=True)
	tmp_folder_path = pathlib.Path(tmp_dir.name)

	# Copy TS file into temp folder
	ts_copy_path = tmp_folder_path / ts_path.name
	shutil.copy(ts_path, ts_copy_path)

	# Run TypeConv with cwd set to temp folder
	subprocess.run([
		"npx", "typeconv",
		"--from-type", "ts",
		"--to-type", "oapi",
		"--output-extension","TYPECONV-GENERATED-FILE-OPENAPI-FILE-EXT",
		"--output-directory", tmp_folder_path,

		"--merge-objects",
		"--strip-annotations",

		ts_copy_path.name
	], check=True, cwd=tmp_folder_path)

	yaml_files = list(tmp_folder_path.glob("*.TYPECONV-GENERATED-FILE-OPENAPI-FILE-EXT"))
	return tmp_dir, str(yaml_files[0])


def generate_and_save_typescript_from_json_file(pb_schema_path):
	with tempfile.NamedTemporaryFile(suffix=".ts", delete=True) as tf:
		tmp_json_file_path = tf.name
	subprocess.run([
		"npx", "pocketbase-typegen",
		"--json", pb_schema_path,
		"--out", tmp_json_file_path
	], check=True)
	return tmp_json_file_path


def generate_pydantic_from_openapi(openapi_path):
	command = [
		"datamodel-codegen",
		"--input-file-type", "openapi",
		"--custom-file-header",
					"\"\\nGenerated models file. \\nDo not edit! \\nChanges are not persistent\\n\"",
		"--use-double-quotes",
		"--input", openapi_path,
		"--use-exact-imports",
		"--use-title-as-name",
		"--no-color",
		# "--keep-model-order",
		# "--use-schema-description",
		# "--use-unique-items-as-set",
		"--collapse-root-models",
		"--strip-default-none",
		# "--snake-case-field",
		"--target-python-version", ".".join(sys.version.split()[0].split(".")[:2]),
	]

	result = subprocess.run(
		command,
		check=True,
		capture_output=True,
		text=True,
	)

	return result.stdout


def remove_classes(ast_tree, classes):
	ast_tree.body = [
		node for node in ast_tree.body
		if not (isinstance(node, ast.ClassDef) and node.name in classes)
	]
	return ast.unparse(ast_tree)

def replace_class_suffixes(ast_tree, sufix_map):
	for node in ast.walk(ast_tree):
		if isinstance(node, ast.ClassDef):
			for old, new in sufix_map.items():
				if node.name.endswith(old):
					node.name = node.name[: -len(old)] + new
					break
	return ast.unparse(ast_tree)

def wire_pbschema_references(pb_schema):
	lookup = {col["id"]: col for col in pb_schema}
	for element in pb_schema:
		fields = element.get("fields", [])
		for field in fields:
			if field.get("type") == "relation":
				parent_id = field.get("collectionId")
				if parent_id in lookup:
					field["collectionRef"] = lookup[parent_id]
	return pb_schema

def replace_pbschema_child_types(ast_tree, pb_schema_with_references):
	"""
	For each ClassDef in ast_tree whose name exists in pb_schema_with_references,
	find class-level annotated fields (AnnAssign). If a field is a relation in the
	schema and points to another collection, replace only the inner type that
	equals a placeholder type (e.g. str) with the target collection class name,
	preserving wrappers like Optional[...] or List[...].
	"""

	# lookups
	name_lookup = {col["name"]: col for col in pb_schema_with_references if "name" in col}
	id_to_name = {col["id"]: col["name"] for col in pb_schema_with_references if "id" in col}

	def ref_to_name(ref):
		"""Return collection name from collectionRef which may be dict with name or id."""
		if isinstance(ref, dict):
			if "name" in ref:
				return ref["name"]
			if "id" in ref and ref["id"] in id_to_name:
				return id_to_name[ref["id"]]
		return None

	def replace_inner(node, new_name):
		"""Recursively replace str annotations inside node with new_name."""
		if node is None:
			return node

		# Replace direct str -> new_name
		if isinstance(node, ast.Name):
			if node.id == "str":
				return ast.copy_location(ast.Name(id=new_name, ctx=ast.Load()), node)
			return node

		if isinstance(node, ast.Attribute):
			if node.attr == "str":
				return ast.copy_location(ast.Name(id=new_name, ctx=ast.Load()), node)
			return node

		if isinstance(node, ast.Subscript):
			new_slice = node.slice
			if isinstance(new_slice, ast.Index):  # <3.9
				new_inner = replace_inner(new_slice.value, new_name)
				new_slice = ast.copy_location(ast.Index(value=new_inner), new_slice)
			else:  # >=3.9
				new_slice = replace_inner(new_slice, new_name)
			return ast.copy_location(
				ast.Subscript(value=node.value, slice=new_slice, ctx=node.ctx),
				node,
			)

		if isinstance(node, ast.Tuple):
			new_elts = [replace_inner(e, new_name) for e in node.elts]
			ctx = getattr(node, "ctx", ast.Load())
			return ast.copy_location(ast.Tuple(elts=new_elts, ctx=ctx), node)

		if isinstance(node, ast.List):
			new_elts = [replace_inner(e, new_name) for e in node.elts]
			ctx = getattr(node, "ctx", ast.Load())
			return ast.copy_location(ast.List(elts=new_elts, ctx=ctx), node)

		return node

	for node in ast.walk(ast_tree):
		if not isinstance(node, ast.ClassDef):
			continue
		if node.name not in name_lookup:
			continue

		schema = name_lookup[node.name]
		field_map = {f["name"]: f for f in schema.get("fields", []) if "name" in f}

		for class_stmt in node.body:
			if not isinstance(class_stmt, ast.AnnAssign):
				continue

			if isinstance(class_stmt.target, ast.Name):
				varname = class_stmt.target.id
			elif isinstance(class_stmt.target, ast.Attribute):
				varname = class_stmt.target.attr
			else:
				continue

			field = field_map.get(varname)
			if not field or field.get("type") != "relation" or not field.get("collectionRef"):
				continue

			target_class = ref_to_name(field["collectionRef"])
			if not target_class:
				continue

			class_stmt.annotation = replace_inner(class_stmt.annotation, target_class)

	ast.fix_missing_locations(ast_tree)
	return ast.unparse(ast_tree)

async def pb_models_to_pydantic_models(url, username, password):
	pb = PocketBase(url)
	await authenticate_pb(pb, username, password)
	pb_schema = await get_pb_schema(pb)
	pb_schema_with_references = wire_pbschema_references(pb_schema)

	schema_filepath = write_pb_schema_to_tmpfile(pb_schema)

	ts_schema_path = generate_and_save_typescript_from_json_file(schema_filepath)
	_,openapi_path = generate_and_save_openapi_from_typescript_path(ts_schema_path)

	code = generate_pydantic_from_openapi(openapi_path)

	ast_tree = ast.parse(code)
	replace_class_suffixes(ast_tree, {"Record": "", "Records": ""})
	replace_pbschema_child_types(ast_tree, pb_schema_with_references)
	res4 = remove_classes(ast_tree, ["TypedPocketBase", "CollectionResponses","Collection"])

	print(res4)


if __name__ == "__main__":

	asyncio.run(pb_models_to_pydantic_models(
		CONNECTION_URL,
		SUPERUSER_EMAIL,
		SUPERUSER_PASSWORD
	))

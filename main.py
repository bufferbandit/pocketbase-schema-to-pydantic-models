import ast
import json
import pathlib
import shutil
import re
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

def replace_ts_enums_with_unions(path_to_ts_file):
	# Read the TS file
	with open(path_to_ts_file, "r", encoding="utf8") as f:
		ts_data = f.read()

	# Map of enum name -> union string
	enum_map = {}

	# Pattern to capture whole enums
	enum_pattern = re.compile(r"export enum (\w+)\s*\{([^}]*)\}", re.DOTALL)

	# Find all enums
	for match in enum_pattern.finditer(ts_data):
		enum_name = match.group(1)
		enum_body = match.group(2)

		# Split into members
		members = [m.strip() for m in enum_body.split(",") if m.strip()]
		values = []
		for m in members:
			if "=" in m:
				# Explicit assignment: take right side
				name, val = [p.strip() for p in m.split("=", 1)]
				# Strip quotes if missing (handle numeric vs string)
				if val.isdigit():
					# numeric enum → use the name instead
					values.append(f'"{name}"')
				else:
					values.append(val)
			else:
				# No assignment → use the name itself as string
				values.append(f'"{m}"')

		union = " | ".join(values)
		enum_map[enum_name] = union

	# Remove enum blocks completely
	ts_data = enum_pattern.sub("", ts_data)

	# Replace references to enums with their unions
	for enum_name, union in enum_map.items():
		ts_data = re.sub(rf"\b{enum_name}\b", union, ts_data)

	# Write back to same file
	with open(path_to_ts_file, "w", encoding="utf8") as f:
		f.write(ts_data)

	return path_to_ts_file

def generate_pydantic_from_openapi(openapi_path):
	command = [
		"datamodel-codegen",
		"--input-file-type", "openapi",
		"--custom-file-header",
					"\"\\nAuto-generated models file. "
					"\\nDO NOT EDIT! "
					"\\nChanges are not persistent and will "
					"be overwritten on re-generation\\n\"",
		"--use-double-quotes",
		"--input", openapi_path,
		"--use-exact-imports",
		"--use-title-as-name",
		"--no-color",
		# "--keep-model-order",
		# "--use-schema-description",
		# "--use-unique-items-as-set",
		"--strip-default-none",


		# "--collapse-root-models",

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
	return ast_tree

def remove_classes_with_suffixes(ast_tree, suffixes):
	ast_tree.body = [
		node for node in ast_tree.body
		if not (isinstance(node, ast.ClassDef) and any(node.name.endswith(suf) for suf in suffixes))
	]
	ast.fix_missing_locations(ast_tree)
	return ast_tree

def replace_class_suffixes(ast_tree, sufix_map):
	for node in ast.walk(ast_tree):
		if isinstance(node, ast.ClassDef):
			for old, new in sufix_map.items():
				if node.name.endswith(old):
					node.name = node.name[: -len(old)] + new
					break
	return ast_tree

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

def remove_config_classes(ast_tree):
	"""
	Remove all inner 'Config' classes from the datamodels for increased readability
	"""
	for cls in ast.walk(ast_tree):
		if not isinstance(cls, ast.ClassDef):
			continue
		# Filter out inner classes named 'Config'
		cls.body = [stmt for stmt in cls.body
					if not (isinstance(stmt, ast.ClassDef) and stmt.name == "Config")]
	ast.fix_missing_locations(ast_tree)
	return ast_tree

def add_imports(ast_tree, import_lines):
	# Parse each line into AST nodes
	import_nodes = []
	for line in import_lines:
		parsed = ast.parse(line).body
		for node in parsed:
			if isinstance(node, (ast.Import, ast.ImportFrom)):
				import_nodes.append(node)

	# Find the index after the last existing import
	last_import_idx = -1
	for i, stmt in enumerate(ast_tree.body):
		if isinstance(stmt, (ast.Import, ast.ImportFrom)):
			last_import_idx = i

	# Insert new imports right after existing ones
	insert_idx = last_import_idx + 1
	ast_tree.body[insert_idx:insert_idx] = import_nodes
	ast.fix_missing_locations(ast_tree)
	return ast_tree

def rename_classes(ast_tree, rename_map):
	"""
	Rename classes in the AST.
	rename_map: dict mapping old class names to new class names, e.g. {"Old": "New"}
	"""
	for node in ast.walk(ast_tree):
		if isinstance(node, ast.ClassDef):
			if node.name in rename_map:
				node.name = rename_map[node.name]

		# Also rename references in annotations if needed
		if isinstance(node, ast.Name):
			if node.id in rename_map:
				node.id = rename_map[node.id]

	ast.fix_missing_locations(ast_tree)
	return ast_tree

def replace_relation_annotations(ast_tree, pb_schema_with_references, classnames_original_collection_names):
	"""
	Recursively replace all occurrences of RecordIdString in type annotations
	with int, preserving wrappers like Optional and List.
	"""

	reversed_dict = {v: k for k, v in classnames_original_collection_names.items()}

	def recursive_replace(node, field_name, parent_class):

		# Base case: replace RecordIdString → int
		if isinstance(node, ast.Name) and node.id == "RecordIdString":
			original_collection_name = reversed_dict[parent_class.name]
			parent_original_schema = next(x for x in pb_schema_with_references if x["name"] == original_collection_name)

			parent_fields = parent_original_schema["fields"]
			field_original_schema = next(filter(lambda f: f["name"] == field_name, parent_fields))


			# pattern
			# min
			# max
			# system
			# hidden
			# presentatble
			# autogeneratePattern
			# type


			schema_collection_ref = field_original_schema.get("collectionRef")
			schema_collection_name = schema_collection_ref["name"]
			class_name = classnames_original_collection_names[schema_collection_name]
			return ast.Name(id=class_name, ctx=ast.Load())

		# Handle subscripted types (Optional[T], List[T], etc.)
		if isinstance(node, ast.Subscript):
			node.value = recursive_replace(node.value, field_name, parent_class)
			# Python <3.9
			if isinstance(node.slice, ast.Index):
				node.slice.value = recursive_replace(node.slice.value, field_name, parent_class)
			else:  # Python 3.9+
				node.slice = recursive_replace(node.slice, field_name, parent_class)
			return node

		# Handle tuple/list of types (e.g., Union)
		if isinstance(node, (ast.Tuple, ast.List)):
			node.elts = [recursive_replace(e,field_name, parent_class) for e in node.elts]
			return node

		return node

	for cls in ast.walk(ast_tree):
		if not isinstance(cls, ast.ClassDef):
			continue
		parent_class = cls
		for stmt in cls.body:
			if not isinstance(stmt, ast.AnnAssign):
				continue
			field_name = getattr(stmt.target, "id", None) or getattr(stmt.target, "attr", None)
			stmt.annotation = recursive_replace(stmt.annotation, field_name, parent_class)

	ast.fix_missing_locations(ast_tree)
	return ast_tree

def get_classnames_original_collection_names(ast_tree):
	"""
	Finds the class named 'Collection' and returns a mapping:
	attribute name → class name with 'Record' suffix removed.
	Example: {'Child': 'Child', 'Parent': 'Parent', ...}
	"""
	mapping = {}

	for cls in ast.walk(ast_tree):
		if isinstance(cls, ast.ClassDef) and cls.name == "Collection":
			for stmt in cls.body:
				if isinstance(stmt, ast.AnnAssign):
					attr_name = getattr(stmt.target, "id", None) or getattr(stmt.target, "attr", None)
					# Get type name and remove 'Record' suffix
					type_name = None
					if isinstance(stmt.annotation, ast.Name):
						type_name = stmt.annotation.id
					elif isinstance(stmt.annotation, ast.Subscript):
						# e.g., Optional[ChildRecord] or List[ChildRecord]
						sub = stmt.annotation
						if isinstance(sub.slice, ast.Index):  # Python <3.9
							type_node = sub.slice.value
						else:  # Python 3.9+
							type_node = sub.slice
						if isinstance(type_node, ast.Name):
							type_name = type_node.id
					if type_name and type_name.endswith("Record"):
						type_name = type_name[:-len("Record")]
					mapping[attr_name] = type_name
			break

	return mapping

def replace_types(ast_tree, rename_map):
	for k,v in rename_map.items():
		remove_classes(ast_tree, [k])
		rename_classes(ast_tree, {k:v})

def merge_all_enum_classes(ast_tree):
	"""
	Merge all Enum classes with numeric suffixes and nest them inside
	the first class that references them.
	"""

	# Step 1: Collect all Enum classes
	enum_classes = [
		node for node in ast_tree.body
		if isinstance(node, ast.ClassDef)
		   and any(isinstance(base, ast.Name) and base.id == "Enum" for base in node.bases)
	]

	# Step 2: Group by base name (strip trailing digits manually)
	groups = {}
	for enum_cls in enum_classes:
		name = enum_cls.name
		i = len(name) - 1
		while i >= 0 and name[i].isdigit():
			i -= 1
		base_name = name[:i+1] if i >= 0 else name
		groups.setdefault(base_name, []).append(enum_cls)

	# Step 3: Merge each group
	for base_name, group in groups.items():
		if len(group) < 1:
			continue

		# Merge members
		merged_members = {}
		for enum_cls in group:
			for stmt in enum_cls.body:
				if isinstance(stmt, ast.Assign):
					for target in stmt.targets:
						if isinstance(target, ast.Name):
							merged_members[target.id] = stmt.value

		merged_enum = ast.ClassDef(
			name=base_name,
			bases=[ast.Name(id="Enum", ctx=ast.Load())],
			keywords=[],
			body=[ast.Assign(targets=[ast.Name(id=k, ctx=ast.Store())], value=v) for k, v in merged_members.items()],
			decorator_list=[]
		)

		# Remove old Enum classes
		ast_tree.body = [node for node in ast_tree.body if node not in group]

		# Step 4: Find first parent class that references any of these enums
		old_names = {node.name for node in group}
		parent_class = None
		for cls in ast_tree.body:
			if not isinstance(cls, ast.ClassDef):
				continue
			for node in ast.walk(cls):
				if isinstance(node, ast.Name) and node.id in old_names:
					parent_class = cls
					break
			if parent_class:
				break

		# Step 5: Nest the merged Enum inside the parent class, or at top if none found
		if parent_class:
			parent_class.body.insert(0, merged_enum)
		else:
			ast_tree.body.insert(0, merged_enum)

		# Step 6: Update type annotations
		for node in ast.walk(ast_tree):
			if isinstance(node, ast.Name) and node.id in old_names:
				node.id = f"{base_name}"  # remains inner class reference in type annotations

	ast.fix_missing_locations(ast_tree)
	return ast_tree

def simplify_enum_union_annotations(ast_tree):
	"""
	Merge multiple enums, nest them in the parent class,
	fix type annotations to point to the nested Enum,
	and collapse repeated Union members.
	"""

	# Step 1: collect enums by parent class
	parent_map = {}  # parent_class_name -> {enum_name: enum_node}
	top_level_enums = []

	for node in ast_tree.body:
		if isinstance(node, ast.ClassDef):
			# look for nested enums
			nested = {}
			for n in node.body:
				if isinstance(n, ast.ClassDef):
					if any(isinstance(base, ast.Name) and base.id == "Enum" for base in n.bases):
						nested[n.name] = n
			if nested:
				parent_map[node.name] = nested

	# Step 2: collect top-level enums that are not nested
	for node in ast_tree.body:
		if isinstance(node, ast.ClassDef):
			if any(isinstance(base, ast.Name) and base.id == "Enum" for base in node.bases):
				top_level_enums.append(node)

	# Step 3: nest all top-level enums into first parent class (e.g., Users)
	if top_level_enums and parent_map:
		first_parent = next(iter(parent_map))
		for enum in top_level_enums:
			# avoid name collisions
			enum_name = enum.name
			existing_names = set(parent_map[first_parent].keys())
			if enum_name in existing_names:
				enum_name = enum_name + "_"
				enum.name = enum_name
			parent_map[first_parent][enum_name] = enum
			# remove from top-level
			ast_tree.body.remove(enum)
			# insert into parent class
			for cls in ast_tree.body:
				if cls.name == first_parent:
					cls.body.append(enum)

	# Step 4: collect all nested enums (full names) for type replacement
	enum_full_names = {}
	for cls in ast_tree.body:
		if isinstance(cls, ast.ClassDef):
			for n in cls.body:
				if isinstance(n, ast.ClassDef):
					if any(isinstance(base, ast.Name) and base.id == "Enum" for base in n.bases):
						enum_full_names[n.name] = cls.name  # enum_name -> parent_class_name

	# Step 5: replace type annotations and collapse duplicates in Union
	class Transformer(ast.NodeTransformer):
		def visit_Name(self, node):
			if node.id in enum_full_names:
				return ast.Attribute(
					value=ast.Name(id=enum_full_names[node.id], ctx=ast.Load()),
					attr=node.id,
					ctx=node.ctx
				)
			return node

		def visit_Subscript(self, node):
			# Recurse first
			self.generic_visit(node)

			# Collapse repeated Enum types in Union
			if isinstance(node.value, ast.Name) and node.value.id == "Union":
				elts = []
				if isinstance(node.slice, ast.Tuple):
					elts = node.slice.elts
				elif isinstance(node.slice, ast.Index) and isinstance(node.slice.value, ast.Tuple):
					elts = node.slice.value.elts
				else:
					elts = [node.slice]

				seen = {}
				for e in elts:
					key = ast.unparse(e)
					# only collapse if it's one of our nested enums
					if key.split(".")[-1] in enum_full_names:
						seen[key] = e
					else:
						seen[key] = e
				elts_unique = list(seen.values())

				# Replace node
				if len(elts_unique) == 1:
					return elts_unique[0]
				else:
					if isinstance(node.slice, ast.Tuple):
						node.slice.elts = elts_unique
					elif isinstance(node.slice, ast.Index):
						node.slice.value.elts = elts_unique
					return node
			return node

	transformer = Transformer()
	transformed_tree = transformer.visit(ast_tree)
	ast.fix_missing_locations(transformed_tree)
	return transformed_tree

#######



async def pb_models_to_pydantic_models(model_out_filename, url, username, password):

	pb = PocketBase(url)
	await authenticate_pb(pb, username, password)

	pb_schema = await get_pb_schema(pb)

	imports = ["from datetime import datetime"]
	type_replacements = {"IsoDateString":"datetime"}

	pb_schema_with_references = wire_pbschema_references(pb_schema)

	schema_filepath = write_pb_schema_to_tmpfile(pb_schema)

	ts_schema_path = generate_and_save_typescript_from_json_file(schema_filepath)

	ts_schema_path = replace_ts_enums_with_unions(ts_schema_path)

	_,openapi_path = generate_and_save_openapi_from_typescript_path(ts_schema_path)

	code = generate_pydantic_from_openapi(openapi_path)

	ast_tree = ast.parse(code)
	replace_class_suffixes(ast_tree, {"Record": "", "Records": ""})
	remove_config_classes(ast_tree)
	add_imports(ast_tree,imports)
	replace_types(ast_tree, type_replacements)

	classnames_original_collection_names = get_classnames_original_collection_names(ast_tree)
	remove_classes(ast_tree, ["Collection"])

	replace_relation_annotations(ast_tree, pb_schema_with_references, classnames_original_collection_names)

	remove_classes(ast_tree, ["TypedPocketBase", "CollectionResponses","RecordIdString"])
	remove_classes_with_suffixes(ast_tree,["Response"])

	merge_all_enum_classes(ast_tree)
	simplify_enum_union_annotations(ast_tree)

	out_transformed_ast = ast.unparse(ast_tree)
	with open(model_out_filename, "w", encoding="utf-8") as f:
		f.write(out_transformed_ast)



if __name__ == "__main__":
	from proj_secrets import MODEL_OUT_FILENAME,CONNECTION_URL, SUPERUSER_EMAIL, SUPERUSER_PASSWORD
	asyncio.run(pb_models_to_pydantic_models(
		MODEL_OUT_FILENAME,
		CONNECTION_URL,
		SUPERUSER_EMAIL,
		SUPERUSER_PASSWORD
	))

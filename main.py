import json
import pathlib
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


def write_pb_schema(pb_schema_data):
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
		"--custom-file-header", "",
		"--use-double-quotes",
		"--input", openapi_path,
		"--use-exact-imports",
		"--use-title-as-name",
		# "--keep-model-order",
		# "--use-schema-description",
		"--collapse-root-models",
		"--target-python-version", ".".join(sys.version.split()[0].split(".")[:2]),
	]

	result = subprocess.run(
		command,
		check=True,
		capture_output=True,
		text=True,
	)

	return result.stdout


async def pb_models_to_pydantic_models(url, username, password):
	pb = PocketBase(url)
	await authenticate_pb(pb, username, password)
	pb_schema = await get_pb_schema(pb)
	schema_filepath = write_pb_schema(pb_schema)

	ts_schema_path = generate_and_save_typescript_from_json_file(schema_filepath)
	_,openapi_path = generate_and_save_openapi_from_typescript_path(ts_schema_path)

	print(run_datamodel_codegen(openapi_path))


if __name__ == "__main__":

	asyncio.run(pb_models_to_pydantic_models(
		CONNECTION_URL,
		SUPERUSER_EMAIL,
		SUPERUSER_PASSWORD
	))

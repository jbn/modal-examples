# ---
# deploy: true
# lambda-test: false
# ---
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import modal

bucket_creds = modal.Secret.from_name(
    "aws-s3-modal-examples-datasets", environment_name="main"
)
bucket_name = "modal-examples-datasets"
volume = modal.CloudBucketMount(
    bucket_name,
    secret=bucket_creds,
)
image = modal.Image.debian_slim().apt_install("wget").pip_install("tqdm")
app = modal.App(
    "example-coco-dataset-import",
    image=image,
    secrets=[],
)


def start_monitoring_disk_space(interval: int = 30) -> None:
    """Start monitoring the disk space in a separate thread."""
    task_id = os.environ["MODAL_TASK_ID"]

    def log_disk_space(interval: int) -> None:
        while True:
            statvfs = os.statvfs("/")
            free_space = statvfs.f_frsize * statvfs.f_bavail
            print(
                f"{task_id} free disk space: {free_space / (1024 ** 3):.2f} GB",
                file=sys.stderr,
            )
            time.sleep(interval)

    monitoring_thread = threading.Thread(
        target=log_disk_space, args=(interval,)
    )
    monitoring_thread.daemon = True
    monitoring_thread.start()


def extractall(fzip, dest, desc="Extracting"):
    from tqdm.auto import tqdm
    from tqdm.utils import CallbackIOWrapper

    dest = pathlib.Path(dest).expanduser()
    with zipfile.ZipFile(fzip) as zipf, tqdm(
        desc=desc,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        total=sum(getattr(i, "file_size", 0) for i in zipf.infolist()),
    ) as pbar:
        for i in zipf.infolist():
            if not getattr(i, "file_size", 0):  # directory
                zipf.extract(i, os.fspath(dest))
            else:
                full_path = dest / i.filename
                full_path.parent.mkdir(exist_ok=True, parents=True)
                with zipf.open(i) as fi, open(full_path, "wb") as fo:
                    shutil.copyfileobj(CallbackIOWrapper(pbar.update, fi), fo)


def copy_concurrent(src: pathlib.Path, dest: pathlib.Path) -> None:
    from multiprocessing.pool import ThreadPool

    class MultithreadedCopier:
        def __init__(self, max_threads):
            self.pool = ThreadPool(max_threads)

        def copy(self, source, dest):
            self.pool.apply_async(shutil.copy2, args=(source, dest))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.pool.close()
            self.pool.join()

    with MultithreadedCopier(max_threads=48) as copier:
        shutil.copytree(
            src, dest, copy_function=copier.copy, dirs_exist_ok=True
        )


# This script uses wget to download ZIP files over HTTP because while the official
# website recommends using gsutil to download from a bucket (https://cocodataset.org/#download)
# that bucket no longer exists.


@app.function(
    volumes={"/vol/": volume},
    timeout=60 * 60 * 4,  # 4 hours
    ephemeral_disk=600 * 1024,  # 600 GiB
)
def import_transform_load() -> None:
    start_monitoring_disk_space()

    train2017_tmp = pathlib.Path("/tmp/train2017.zip")
    val2017_tmp = pathlib.Path("/tmp/val2017.zip")
    test2017_tmp = pathlib.Path("/tmp/test2017.zip")
    unlabeled2017_tmp = pathlib.Path("/tmp/unlabeled2017.zip")
    annotations_trainval2017 = pathlib.Path("/tmp/annotations_trainval2017.zip")
    stuff_annotations_trainval2017 = pathlib.Path(
        "/tmp/stuff_annotations_trainval2017.zip"
    )
    image_info_test2017 = pathlib.Path("/tmp/image_info_test2017.zip")
    image_info_unlabeled2017 = pathlib.Path("/tmp/image_info_unlabeled2017.zip")
    commands = [
        f"wget http://images.cocodataset.org/zips/train2017.zip -O {train2017_tmp}",
        f"wget http://images.cocodataset.org/zips/val2017.zip -O {val2017_tmp}",
        f"wget http://images.cocodataset.org/zips/test2017.zip -O {test2017_tmp}",
        f"wget http://images.cocodataset.org/zips/unlabeled2017.zip -O {unlabeled2017_tmp}",
        f"wget http://images.cocodataset.org/annotations/annotations_trainval2017.zip -O {annotations_trainval2017}",
        f"wget http://images.cocodataset.org/annotations/stuff_annotations_trainval2017.zip -O {stuff_annotations_trainval2017}",
        f"wget http://images.cocodataset.org/annotations/image_info_test2017.zip -O {image_info_test2017}",
        f"wget http://images.cocodataset.org/annotations/image_info_unlabeled2017.zip -O {image_info_unlabeled2017}",
    ]
    # Start all downloads in parallel
    processes = [subprocess.Popen(cmd, shell=True) for cmd in commands]
    # Wait for all downloads to complete
    errors = []
    for p in processes:
        returncode = p.wait()
        if returncode == 0:
            print("Download completed successfully.")
        else:
            errors.append(
                f"Error in downloading. {p.args!r} failed {returncode=}"
            )
    if errors:
        raise RuntimeError(errors)

    destination = pathlib.Path("/tmp/train2017/")
    for (
        src,
        extract_dest,
        final_dest,
    ) in [
        (
            train2017_tmp,
            pathlib.Path("/tmp/train2017/"),
            pathlib.Path("/vol/coco/train2017/"),
        ),
        (
            val2017_tmp,
            pathlib.Path("/tmp/val2017/"),
            pathlib.Path("/vol/coco/val2017/"),
        ),
        (
            test2017_tmp,
            pathlib.Path("/tmp/test2017/"),
            pathlib.Path("/vol/coco/test2017/"),
        ),
        (
            unlabeled2017_tmp,
            pathlib.Path("/tmp/unlabeled2017/"),
            pathlib.Path("/vol/coco/unlabeled2017/"),
        ),
        (
            annotations_trainval2017,
            pathlib.Path("/tmp/annotations_trainval2017/"),
            pathlib.Path("/vol/coco/annotations_trainval2017/"),
        ),
        (
            stuff_annotations_trainval2017,
            pathlib.Path("/tmp/stuff_annotations_trainval2017/"),
            pathlib.Path("/vol/coco/stuff_annotations_trainval2017/"),
        ),
        (
            image_info_test2017,
            pathlib.Path("/tmp/image_info_test2017/"),
            pathlib.Path("/vol/coco/image_info_test2017/"),
        ),
        (
            image_info_unlabeled2017,
            pathlib.Path("/tmp/image_info_unlabeled2017/"),
            pathlib.Path("/vol/coco/image_info_unlabeled2017/"),
        ),
    ]:
        extract_dest.mkdir()
        extractall(src, destination)  # extract into /tmp/
        src.unlink()  # free up disk space by deleting the zip
        copy_concurrent(
            destination, final_dest
        )  # copy from /tmp/ into mounted volume
    print("✅ Done")
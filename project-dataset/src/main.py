import os
import yaml
import pathlib
import sys
from collections import defaultdict
import random
import supervisely_lib as sly

root_source_path = str(pathlib.Path(sys.argv[0]).parents[2])
sly.logger.info(f"Root source directory: {root_source_path}")
sys.path.append(root_source_path)

from shared_utils.connect import get_model_info
from shared_utils.inference import postprocess
import init_ui as ui

owner_id = int(os.environ['context.userId'])
team_id = int(os.environ['context.teamId'])
project_id = int(os.environ["modal.state.slyProjectId"])

my_app: sly.AppService = sly.AppService(ignore_task_id=True)
model_meta: sly.ProjectMeta = None

ann_cache = defaultdict(list)  # only one (current) image in cache
project_info = None
project_images = None
project_meta: sly.ProjectMeta = None

image_grid_options = {
    "opacity": 0.5,
    "fillRectangle": False,
    "enableZoom": True,
    "syncViews": True,
    "showPreview": True,
    "selectable": False
}

empty_gallery = {
    "content": {
        "projectMeta": {},
        "annotations": {},
        "layout": []
    },
    "options": image_grid_options,
}


@my_app.callback("connect")
@sly.timeit
def connect(api: sly.Api, task_id, context, state, app_logger):
    global model_meta
    model_meta = get_model_info(api, task_id, context, state, app_logger)


@my_app.callback("disconnect")
@sly.timeit
def disconnect(api: sly.Api, task_id, context, state, app_logger):
    global model_meta
    model_meta = None

    new_data = {}
    new_state = {}
    ui.init(new_data, new_state)
    fields = [
        {"field": "data", "payload": new_data, "append": True},
        {"field": "state", "payload": new_state, "append": True},
    ]
    api.task.set_fields(task_id, fields)


@my_app.callback("select_all_classes")
@sly.timeit
def select_all_classes(api: sly.Api, task_id, context, state, app_logger):
    api.task.set_field(task_id, "state.classes", [True] * len(model_meta.obj_classes))


@my_app.callback("deselect_all_classes")
@sly.timeit
def deselect_all_classes(api: sly.Api, task_id, context, state, app_logger):
    api.task.set_field(task_id, "state.classes", [False] * len(model_meta.obj_classes))


@my_app.callback("select_all_tags")
@sly.timeit
def select_all_tags(api: sly.Api, task_id, context, state, app_logger):
    api.task.set_field(task_id, "state.tags", [True] * len(model_meta.tag_metas))


@my_app.callback("deselect_all_tags")
@sly.timeit
def deselect_all_tags(api: sly.Api, task_id, context, state, app_logger):
    api.task.set_field(task_id, "state.tags", [False] * len(model_meta.tag_metas))


@my_app.callback("inference")
@sly.timeit
def inference(api: sly.Api, task_id, context, state, app_logger):
    project_id = context["projectId"]
    image_id = context["imageId"]

    try:
        inference_setting = yaml.safe_load(state["settings"])
    except Exception as e:
        inference_setting = {}
        app_logger.warn(repr(e))

    project_meta = sly.ProjectMeta.from_json(api.project.get_meta(project_id))

    if image_id not in ann_cache:
        # keep only current image for simplicity
        ann_cache.clear()

    ann_json = api.annotation.download(image_id).annotation
    ann = sly.Annotation.from_json(ann_json, project_meta)
    ann_cache[image_id].append(ann)

    ann_pred_json = api.task.send_request(state["sessionId"],
                                          "inference_image_id",
                                          data={
                                              "image_id": image_id,
                                              "settings": inference_setting
                                          })
    ann_pred = sly.Annotation.from_json(ann_pred_json, model_meta)
    res_ann: sly.Annotation = postprocess(api, project_id, ann_pred, project_meta, model_meta, state)

    if state["addMode"] == "merge":
        res_ann = ann.merge(res_ann)
    else:
        pass  # replace (data prepared, nothing to do)

    api.annotation.upload_ann(image_id, res_ann)
    fields = [
        {"field": "data.rollbackIds", "payload": list(ann_cache.keys())},
        {"field": "state.processing", "payload": False}
    ]
    api.task.set_fields(task_id, fields)


@my_app.callback("preview")
@sly.timeit
def preview(api: sly.Api, task_id, context, state, app_logger):
    try:
        inference_setting = yaml.safe_load(state["settings"])
    except Exception as e:
        inference_setting = {}
        app_logger.warn(repr(e))

    image_info = random.choice(project_images)
    img = api.image.download_np(image_info.id)

    ann_json = api.annotation.download(image_info.id).annotation
    ann = sly.Annotation.from_json(ann_json, project_meta)

    ann_pred_json = api.task.send_request(state["sessionId"], "inference_image_id",
                                          data={
                                              "image_id": image_info.id,
                                              "settings": inference_setting
                                          })
    ann_pred = sly.Annotation.from_json(ann_pred_json, model_meta)
    res_ann, res_project_meta = postprocess(api, project_id, ann_pred, project_meta, model_meta, state)

    if state["addMode"] == "merge":
        res_ann = ann.merge(res_ann)
    else:
        pass  # replace (data prepared, nothing to do)

    preview_gallery = {
        "content": {
            "projectMeta": res_project_meta.to_json(),
            "annotations": {
                "original": {
                    "url": image_info.full_storage_url,
                    "figures": [label.to_json() for label in ann.labels],
                    "info": {
                        "title": "original",
                    }
                },
                "prediction": {
                    "url": image_info.full_storage_url,
                    "figures": [label.to_json() for label in res_ann.labels],
                    "info": {
                        "title": "prediction",
                    }
                }
            },
            "layout": [["original", "prediction"]]
        },
        "options": image_grid_options,
    }

    fields = [
        {"field": "state.processing", "payload": False},
        {"field": "data.gallery", "payload": preview_gallery}
    ]
    api.task.set_fields(task_id, fields)


def main():
    data = {}
    state = {}
    data["ownerId"] = owner_id
    data["teamId"] = team_id

    global project_info
    project_info = my_app.public_api.project.get_info_by_id(project_id)

    global project_images
    project_images = []
    for dataset_info in my_app.public_api.dataset.get_list(project_id):
        project_images.extend(my_app.public_api.image.get_list(dataset_info.id))

    global project_meta
    project_meta = sly.ProjectMeta.from_json(my_app.public_api.project.get_meta(project_id))

    ui.init(data, state)
    data["emptyGallery"] = empty_gallery
    ui.init_input_project(my_app.public_api, data, project_info)
    ui.init_output_project(data)

    my_app.run(data=data, state=state)


if __name__ == "__main__":
    sly.main_wrapper("main", main)

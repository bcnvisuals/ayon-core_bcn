import collections
import os
import uuid
from typing import List, Dict, Any

import clique
import ayon_api
from ayon_api.operations import OperationsSession
import qargparse
from qtpy import QtWidgets, QtCore

from ayon_core import style
from ayon_core.lib import format_file_size
from ayon_core.pipeline import load, Anatomy
from ayon_core.pipeline.load import (
    get_representation_path_with_anatomy,
    InvalidRepresentationContext,
)


class DeleteOldVersions(load.ProductLoaderPlugin):
    """Deletes specific number of old version"""

    is_multiple_contexts_compatible = True
    sequence_splitter = "__sequence_splitter__"

    representations = ["*"]
    product_types = {"*"}
    tool_names = ["library_loader"]

    label = "Delete Old Versions"
    order = 35
    icon = "trash"
    color = "#d8d8d8"

    options = [
        qargparse.Integer(
            "versions_to_keep", default=2, min=0, help="Versions to keep:"
        ),
        qargparse.Boolean(
            "remove_publish_folder", help="Remove publish folder:"
        ),
        qargparse.Boolean(
            "select_version_ids", default=False,
            help="Select specific versions to delete (overrides 'versions to keep')."
        )
    ]

    requires_confirmation = True

    def delete_whole_dir_paths(self, dir_paths, delete=True):
        size = 0

        for dir_path in dir_paths:
            # Delete all files and folders in dir path
            for root, dirs, files in os.walk(dir_path, topdown=False):
                for name in files:
                    file_path = os.path.join(root, name)
                    size += os.path.getsize(file_path)
                    if delete:
                        os.remove(file_path)
                        self.log.debug("Removed file: {}".format(file_path))

                for name in dirs:
                    if delete:
                        os.rmdir(os.path.join(root, name))

            if not delete:
                continue

            # Delete even the folder and it's parents folders if they are empty
            while True:
                if not os.path.exists(dir_path):
                    dir_path = os.path.dirname(dir_path)
                    continue

                if len(os.listdir(dir_path)) != 0:
                    break

                os.rmdir(os.path.join(dir_path))

        return size

    def path_from_representation(self, representation, anatomy):
        try:
            context = representation["context"]
        except KeyError:
            return (None, None)

        try:
            path = get_representation_path_with_anatomy(
                representation, anatomy
            )
        except InvalidRepresentationContext:
            return (None, None)

        sequence_path = None
        if "frame" in context:
            context["frame"] = self.sequence_splitter
            sequence_path = get_representation_path_with_anatomy(
                representation, anatomy
            )

        if sequence_path:
            sequence_path = sequence_path.normalized()

        return (path.normalized(), sequence_path)

    def delete_only_repre_files(self, dir_paths, file_paths, delete=True):
        size = 0

        for dir_id, dir_path in dir_paths.items():
            dir_files = os.listdir(dir_path)
            collections, remainders = clique.assemble(dir_files)
            for file_path, seq_path in file_paths[dir_id]:
                file_path_base = os.path.split(file_path)[1]
                # Just remove file if `frame` key was not in context or
                # filled path is in remainders (single file sequence)
                if not seq_path or file_path_base in remainders:
                    if not os.path.exists(file_path):
                        self.log.debug(
                            "File was not found: {}".format(file_path)
                        )
                        continue

                    size += os.path.getsize(file_path)

                    if delete:
                        os.remove(file_path)
                        self.log.debug("Removed file: {}".format(file_path))

                    if file_path_base in remainders:
                        remainders.remove(file_path_base)
                    continue

                seq_path_base = os.path.split(seq_path)[1]
                head, tail = seq_path_base.split(self.sequence_splitter)

                final_col = None
                for collection in collections:
                    if head != collection.head or tail != collection.tail:
                        continue
                    final_col = collection
                    break

                if final_col is not None:
                    # Fill full path to head
                    final_col.head = os.path.join(dir_path, final_col.head)
                    for _file_path in final_col:
                        if os.path.exists(_file_path):

                            size += os.path.getsize(_file_path)

                            if delete:
                                os.remove(_file_path)
                                self.log.debug(
                                    "Removed file: {}".format(_file_path)
                                )

                    _seq_path = final_col.format("{head}{padding}{tail}")
                    self.log.debug("Removed files: {}".format(_seq_path))
                    collections.remove(final_col)

                elif os.path.exists(file_path):
                    size += os.path.getsize(file_path)

                    if delete:
                        os.remove(file_path)
                        self.log.debug("Removed file: {}".format(file_path))
                else:
                    self.log.debug(
                        "File was not found: {}".format(file_path)
                    )

        # Delete as much as possible parent folders
        if not delete:
            return size

        for dir_path in dir_paths.values():
            while True:
                if not os.path.exists(dir_path):
                    dir_path = os.path.dirname(dir_path)
                    continue

                if len(os.listdir(dir_path)) != 0:
                    break

                self.log.debug("Removed folder: {}".format(dir_path))
                os.rmdir(dir_path)

        return size

    def message(self, text):
        msgBox = QtWidgets.QMessageBox()
        msgBox.setText(text)
        msgBox.setStyleSheet(style.load_stylesheet())
        msgBox.setWindowFlags(
            msgBox.windowFlags() | QtCore.Qt.FramelessWindowHint
        )
        msgBox.exec_()

    def _confirm_delete(self,
                        contexts: List[Dict[str, Any]],
                        versions_to_keep: int) -> bool:
        """Prompt user for a deletion confirmation"""

        contexts_list = "\n".join(sorted(
            "- {folder[name]} > {product[name]}".format_map(context)
            for context in contexts
        ))
        num_contexts = len(contexts)
        s = "s" if num_contexts > 1 else ""
        text = (
            "Are you sure you want to delete versions?\n\n"
            f"This will keep only the last {versions_to_keep} "
            f"versions for the {num_contexts} selected product{s}."
        )
        informative_text="Warning: This will delete files from disk"
        detailed_text = (
            f"Keep only {versions_to_keep} versions for:\n{contexts_list}"
        )

        messagebox = QtWidgets.QMessageBox()
        messagebox.setIcon(QtWidgets.QMessageBox.Warning)
        messagebox.setWindowTitle("Delete Old Versions")
        messagebox.setText(text)
        messagebox.setInformativeText(informative_text)
        messagebox.setDetailedText(detailed_text)
        messagebox.setStandardButtons(
            QtWidgets.QMessageBox.Yes
            | QtWidgets.QMessageBox.Cancel
        )
        messagebox.setDefaultButton(QtWidgets.QMessageBox.Cancel)
        messagebox.setStyleSheet(style.load_stylesheet())
        messagebox.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        return messagebox.exec_() == QtWidgets.QMessageBox.Yes

    def _prompt_version_selection(self, versions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Pop up a dialog with a list of versions for the user to select.

        :param versions: A list of version dictionaries.
        :return: A list of the chosen version dictionaries.
        """
        dialog = QtWidgets.QDialog()
        dialog.setWindowTitle("Select Versions to Delete")
        dialog.setWindowFlags(dialog.windowFlags() | QtCore.Qt.FramelessWindowHint)

        layout = QtWidgets.QVBoxLayout(dialog)
        
        info_label = QtWidgets.QLabel("Select the versions you want to delete below:")
        layout.addWidget(info_label)

        # A QListWidget with check boxes for each version
        list_widget = QtWidgets.QListWidget(dialog)
        list_widget.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        layout.addWidget(list_widget)

        for version in versions:
            item_text = f"Version {version['version']} (ID: {version['id']})"
            item = QtWidgets.QListWidgetItem(item_text, list_widget)
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Unchecked)

        # Button box
        button_box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        layout.addWidget(button_box)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)

        dialog.setLayout(layout)
        
        chosen_versions = []
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            # Collect all checked items
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                if item.checkState() == QtCore.Qt.Checked:
                    # Parse out the version info
                    # e.g. "Version X (ID: 12345)" -> you can map it back
                    # Since we placed them in the same order, just index
                    chosen_versions.append(versions[i])

        return chosen_versions

    def get_data(self, context, versions_count):
        """
        Gather data for deletion. If 'select_version_ids' option is on,
        we show a version selection dialog; otherwise, we fall back
        to existing logic (delete all except some number of latest).
        """
        product_entity = context["product"]
        folder_entity = context["folder"]
        project_name = context["project"]["name"]
        anatomy = Anatomy(project_name, project_entity=context["project"])

        # Fetch the 'select_version_ids' setting from context's options if present
        # This is how we read an extra option without breaking existing calls:
        select_version_ids = False
        if context.get("options"):
            select_version_ids = context["options"].get("select_version_ids", False)

        version_fields = ayon_api.get_default_fields_for_type("version")
        version_fields.add("tags")
        versions = list(ayon_api.get_versions(
            project_name,
            product_ids=[product_entity["id"]],
            active=None,
            hero=False,
            fields=version_fields
        ))
        self.log.debug(
            "Version Number ({})".format(len(versions))
        )

        # If user opted to select which versions to delete,
        # we open a small UI and let them pick. Otherwise, do the older logic
        if select_version_ids:
            self.log.debug("User chose to manually select versions to delete.")
            # Present dialog for manual selection
            chosen_versions = self._prompt_version_selection(versions)
            # Filter out chosen versions from the full list. We'll only delete these.
            versions_to_delete = [v for v in versions if v in chosen_versions]
        else:
            # Original logic: keep `versions_count` newest versions, delete the rest
            versions_by_parent = collections.defaultdict(list)
            for ent in versions:
                versions_by_parent[ent["productId"]].append(ent)

            def sort_func(ent):
                return int(ent["version"])

            all_last_versions = []
            for _parent_id, _versions in versions_by_parent.items():
                for idx, version in enumerate(
                    sorted(_versions, key=sort_func, reverse=True)
                ):
                    if idx >= versions_count:
                        break
                    all_last_versions.append(version)

                # Filter out the "latest" versions from the overall list
                for version in all_last_versions:
                    versions.remove(version)

                # Filter out already deleted versions
                versions_to_delete = []
                for version in versions:
                    if "deleted" in version["tags"]:
                        self.log.debug(
                            "Skipping version. Already tagged as inactive. < {} / {} / {} >".format(
                                folder_entity["path"], product_entity["name"], version["version"]
                            )
                        )
                    else:
                        versions_to_delete.append(version)

        # If nothing to delete, return
        if not versions_to_delete:
            msg = "Skipping processing. Nothing to delete on {}/{}".format(
                folder_entity["path"], product_entity["name"]
            )
            self.log.info(msg)
            print(msg)
            return

        # Collect their representations
        version_ids = [v["id"] for v in versions_to_delete]
        self.log.debug(
            "Filtered versions to delete ({})".format(len(version_ids))
        )
        repres = list(ayon_api.get_representations(
            project_name, version_ids=version_ids
        ))
        self.log.debug(
            "Collected representations to remove ({})".format(len(repres))
        )

        dir_paths = {}
        file_paths_by_dir = collections.defaultdict(list)
        for repre in repres:
            file_path, seq_path = self.path_from_representation(
                repre, anatomy
            )
            if file_path is None:
                self.log.debug((
                    "Could not format path for representation \"{}\""
                ).format(str(repre)))
                continue

            dir_path = os.path.dirname(file_path)
            dir_id = None
            for _dir_id, _dir_path in dir_paths.items():
                if _dir_path == dir_path:
                    dir_id = _dir_id
                    break

            if dir_id is None:
                dir_id = uuid.uuid4()
                dir_paths[dir_id] = dir_path

            file_paths_by_dir[dir_id].append([file_path, seq_path])

        dir_ids_to_pop = []
        for dir_id, dir_path in dir_paths.items():
            if os.path.exists(dir_path):
                continue
            dir_ids_to_pop.append(dir_id)

        for dir_id in dir_ids_to_pop:
            dir_paths.pop(dir_id)
            paths = file_paths_by_dir.pop(dir_id)
            paths_msg = ", ".join([
                "'{}'".format(path[0].replace("\\", "/")) for path in paths
            ])
            self.log.debug((
                "Folder does not exist. Deleting its files skipped: {}"
            ).format(paths_msg))

        return {
            "dir_paths": dir_paths,
            "file_paths_by_dir": file_paths_by_dir,
            "versions": versions_to_delete,
            "folder": folder_entity,
            "product": product_entity,
            "archive_product": versions_count == 0
        }

    def main(self, project_name, data, remove_publish_folder):
        # Size of files.
        size = 0
        if not data:
            return size

        if remove_publish_folder:
            size = self.delete_whole_dir_paths(data["dir_paths"].values())
        else:
            size = self.delete_only_repre_files(
                data["dir_paths"], data["file_paths_by_dir"]
            )

        op_session = OperationsSession()
        for version in data["versions"]:
            orig_version_tags = version["tags"]
            version_tags = list(orig_version_tags)
            changes = {}
            if "deleted" not in version_tags:
                version_tags.append("deleted")
                changes["tags"] = version_tags

            if version["active"]:
                changes["active"] = False

            if not changes:
                continue
            op_session.update_entity(
                project_name, "version", version["id"], changes
            )

        op_session.commit()

        return size

    def load(self, contexts, name=None, namespace=None, options=None):
        # Get user options
        versions_to_keep = 2
        remove_publish_folder = False
        select_version_ids = False
        if options:
            versions_to_keep = options.get(
                "versions_to_keep", versions_to_keep
            )
            remove_publish_folder = options.get(
                "remove_publish_folder", remove_publish_folder
            )
            select_version_ids = options.get(
                "select_version_ids", select_version_ids
            )

        # Only show the standard confirmation if we're not doing version selection
        if (
                self.requires_confirmation
                and not select_version_ids
                and not self._confirm_delete(contexts, versions_to_keep)
        ):
            return

        try:
            size = 0
            for count, context in enumerate(contexts):
                # Add options to context so get_data can access them
                context["options"] = {
                    "versions_to_keep": versions_to_keep,
                    "remove_publish_folder": remove_publish_folder,
                    "select_version_ids": select_version_ids
                }
                data = self.get_data(context, versions_to_keep)
                if not data:
                    continue
                project_name = context["project"]["name"]
                size += self.main(project_name, data, remove_publish_folder)
                print("Progressing {}/{}".format(count + 1, len(contexts)))

            msg = "Total size of files: {}".format(format_file_size(size))
            self.log.info(msg)
            self.message(msg)

        except Exception:
            self.log.error("Failed to delete versions.", exc_info=True)


class CalculateOldVersions(DeleteOldVersions):
    """Calculate file size of old versions"""
    label = "Calculate Old Versions"
    order = 30
    tool_names = ["library_loader"]

    options = [
        qargparse.Integer(
            "versions_to_keep", default=2, min=0, help="Versions to keep:"
        ),
        qargparse.Boolean(
            "remove_publish_folder", help="Remove publish folder:"
        )
    ]

    requires_confirmation = False

    def main(self, project_name, data, remove_publish_folder):
        size = 0

        if not data:
            return size

        if remove_publish_folder:
            size = self.delete_whole_dir_paths(
                data["dir_paths"].values(), delete=False
            )
        else:
            size = self.delete_only_repre_files(
                data["dir_paths"], data["file_paths_by_dir"], delete=False
            )

        return size

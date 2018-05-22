#    MIT License
#
#    Copyright (c) 2018 Olaf Haag
#
#    Permission is hereby granted, free of charge, to any person obtaining a copy
#    of this software and associated documentation files (the "Software"), to deal
#    in the Software without restriction, including without limitation the rights
#    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#    copies of the Software, and to permit persons to whom the Software is
#    furnished to do so, subject to the following conditions:
#
#    The above copyright notice and this permission notice shall be included in all
#    copies or substantial portions of the Software.
#
#    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#    SOFTWARE.
__author__ = "Olaf Haag"
__copyright__ = "Olaf Haag"
__credits__ = ['Alex Forsythe']  # for serialization and de-serialization of FCurves.

import os
from collections import OrderedDict
from ctypes import windll, pointer, c_long, c_ulong, Structure

# Import MotionBuilder libraries
from pyfbsdk import *
from pyfbsdk_additions import *


# ---HELPER FUNCTIONS---
class Nonlocals(object):
    """Helper class to implement nonlocal names in Python 2.x"""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _point_t(Structure):
    """Helper structure to get mouse screen coordinates."""
    _fields_ = [('x', c_long), ('y', c_long), ]


def get_takes():
    """Returns a list with FBTake objects.
    :rtype: list
    """
    return FBSystem().Scene.Takes


def has_keys(anim_node, res=False):
    """Looks for keys on AnimationNode and its children.
    :rtype: bool
    """
    if anim_node.KeyCount:
        res = True
    else:
        for sub_node in anim_node.Nodes:
            res = res or has_keys(sub_node)
            if res:
                break
    return res


def get_animated_components():
    """Return scene components that have AnimationNodes and keys on the current layer."""
    animated = list()
    for comp in FBSystem().Scene.Components:
        try:
            if has_keys(comp.AnimationNode):
                animated.append(comp)
        except AttributeError:
            continue
    return animated


def serialize_curve(fcurve):
    """Returns a list of dictionaries representing each of the keys in the given FCurve."""
    key_data_list = []
    
    for key in fcurve.Keys:
        key_data = {
            'time': key.Time.Get(),
            'value': key.Value,
            'interpolation': int(key.Interpolation),
            'tangent-mode': int(key.TangentMode),
            'constant-mode': int(key.TangentConstantMode),
            'left-derivative': key.LeftDerivative,
            'right-derivative': key.RightDerivative,
            'left-weight': key.LeftTangentWeight,
            'right-weight': key.RightTangentWeight
        }
        
        key_data_list.append(key_data)
    
    return key_data_list


def tangent_weight_is_default(tangent_weight):
    """Returns whether the given tangent weight is equal to the default value of
    1/3, taking floating-point precision into account.
    """
    return 0.3333 < tangent_weight < 0.3334


def deserialize_curve(fcurve, key_data_list):
    """Populates the given FCurve based on keyframe data listed in serialized
    form. Expects key data to be ordered by time. Any existing keys will be
    removed from the curve.
    """
    # Ensure a blank slate
    fcurve.EditClear()
    
    # Loop 1: Add keys and set non-numeric properties
    for key_data in key_data_list:
        
        key_index = fcurve.KeyAdd(FBTime(key_data['time']), key_data['value'])
        key = fcurve.Keys[key_index]
        
        key.Interpolation = FBInterpolation.values[key_data['interpolation']]
        key.TangentMode = FBTangentMode.values[key_data['tangent-mode']]
        if key.TangentMode == FBTangentMode.kFBTangentModeTCB:
            key.TangentMode = FBTangentMode.kFBTangentModeBreak
        key.TangentConstantMode = \
            FBTangentConstantMode.values[key_data['constant-mode']]
    
    # Loop 2: With all keys in place, set tangent properties
    for i in range(0, len(key_data_list)):
        
        key_data = key_data_list[i]
        key = fcurve.Keys[i]
        
        key.LeftDerivative = key_data['left-derivative']
        key.RightDerivative = key_data['right-derivative']
        
        if not tangent_weight_is_default(key_data['left-weight']):
            key.LeftTangentWeight = key_data['left-weight']
        if not tangent_weight_is_default(key_data['right-weight']):
            key.RightTangentWeight = key_data['right-weight']


def get_serialized_fcurves(component):
    """Get properties' AnimationNodes and their serialized FCurves.
    Returns a dictionary with AnimationNodes as keys and the respective serialized FCurve as the values.
    :rtype: dict
    """
    # Todo: Get layer weight animation.
    root_anim_node = component.AnimationNode
    # Initialize props for first iteration.
    props = dict()
    
    def fill_props_recursively(anim_node):
        # Do we have a leaf of the AnimationNodes hierarchy?
        if anim_node.FCurve:
            # Make sure FCurve has keys.
            if anim_node.FCurve.Keys:
                # AnimationNodes are persistent across takes/layers. That's why we can use them as keys.
                props.update({anim_node: serialize_curve(anim_node.FCurve)})
        else:
            # We are dealing with a parent node. Let's look at its children nodes.
            for node in anim_node.Nodes:
                # Recursively call ourselves to deal with sub-nodes.
                fill_props_recursively(node)
    
    fill_props_recursively(root_anim_node)
    return props


def set_timespan(num_frames):
    """Takes the current local time as start and sets stop after num_frames."""
    curr_take = FBSystem().CurrentTake
    start_frame = FBSystem().LocalTime().GetFrame()
    stop_frame = start_frame + num_frames
    curr_take.LocalTimeSpan = FBTimeSpan(FBSystem().LocalTime,
                                         FBTime(0, 0, 0, stop_frame, 0))
    
    
###############################################################
# User Interface                                              #
###############################################################
def populate_tool(main_layout):
    """Sets up the GUI elements of the tool.
    :param main_layout: The FBTool
    """
    # Ordered dictionaries for more intuitive batch renaming results.
    nl = Nonlocals(nodes_src=OrderedDict(),  # Which source tree nodes connect to which takes/layers.
                   nodes_dst=OrderedDict(),  # Which destination tree nodes connect to which takes.
                   start_frame=0,
                   stop_frame=100,
                   render_format="mov",
                   )
    
    # A few visual components we'll need defined before using in functions. Just a convenience.
    
    # Start frame for LocalTimeSpan
    start_edit = FBEditNumber()
    start_edit.Min = 0
    start_edit.Max = nl.stop_frame - 1
    start_edit.Precision = 1
    start_edit.SmallStep = 1
    start_edit.LargeStep = 5
    start_edit.Value = nl.start_frame

    # End frame for LocalTimeSpan
    stop_edit = FBEditNumber()
    stop_edit.Min = nl.start_frame + 1
    stop_edit.Precision = 1
    stop_edit.SmallStep = 1
    stop_edit.LargeStep = 5
    stop_edit.Value = nl.stop_frame
    
    # Tree that shows takes and their animation layers that are the source for a transfer.
    tree_src = FBTree()
    tree_src.CheckBoxes = True
    tree_src.AutoExpandOnDblClick = True
    tree_src.AutoExpandOnDragOver = True
    tree_src.AutoScroll = True
    tree_src.AutoScrollOnExpand = True
    tree_src.ItemHeight = 40
    tree_src.DeselectOnCollapse = True
    tree_src.EditNodeOn2Select = True
    tree_src.HighlightOnRightClick = False
    tree_src.MultiDrag = False
    tree_src.MultiSelect = False
    tree_src.NoSelectOnDrag = True
    tree_src.NoSelectOnRightClick = True
    tree_src.ShowLines = True

    # Tree that shows takes that are the destination for layers in a transfer.
    tree_dst = FBTree()
    tree_dst.Caption = "Takes"
    tree_dst.CheckBoxes = True
    tree_dst.AutoExpandOnDblClick = False
    tree_dst.AutoExpandOnDragOver = False
    tree_dst.AutoScroll = True
    tree_dst.AutoScrollOnExpand = True
    tree_dst.ItemHeight = 40
    tree_dst.DeselectOnCollapse = True
    tree_dst.EditNodeOn2Select = True
    tree_dst.HighlightOnRightClick = False
    tree_dst.MultiDrag = False
    tree_dst.MultiSelect = False
    tree_dst.NoSelectOnDrag = True
    tree_dst.NoSelectOnRightClick = True
    tree_dst.ShowLines = True
    
    def reload_src_tree():
        """Populate tree view with takes an their animation layers."""
        tree_src.Clear()
        nl.nodes_src.clear()
        root = tree_src.GetRoot()
        takes = get_takes()
        for take in takes:
            node = tree_src.InsertLast(root, take.Name)
            node.Checked = False
            children = list()
            nl.nodes_src[node] = {'data': take,
                                  'parent': root,
                                  'children': children}
            
            # On to the animation layers.
            # Todo: Child layers of layers.
            num_layers = take.GetLayerCount()
            for i in xrange(num_layers - 1, -1, -1):  # Reverse order to reflect GUI order
                layer = take.GetLayer(i)
                # Setting visuals for layer.
                layer_node = tree_src.InsertLast(node, layer.Name)
                layer_node.Checked = False
                # This is a child of take.
                children.append(layer_node)
                # Connect the node to the layer.
                nl.nodes_src[layer_node] = {'data': layer,
                                            'parent': node}
    
    def reload_dst_tree():
        """Populate tree view with takes."""
        tree_dst.Clear()
        nl.nodes_dst.clear()
        root = tree_dst.GetRoot()
        takes = get_takes()
        for take in takes:
            node = tree_dst.InsertLast(root, take.Name)
            node.Checked = False
            # Connect the node to the data.
            nl.nodes_dst.update({node: take})
            
    def reload_trees(*args):  # Dummy args that come from file operations.
        reload_src_tree()
        reload_dst_tree()
    
    # Populate trees.
    reload_trees()
    # When the application loads another file, update the trees.
    FBApplication().OnFileNewCompleted.Add(reload_trees)
    FBApplication().OnFileOpenCompleted.Add(reload_trees)
    
    '''******************#
    # Callback Functions #
    #******************'''
    def transfer_anim_layers():
        """Copies checked animation layers from source panel to checked takes in destination panel."""
        # Todo: Whew, that's quite a number of nested for-loops. Is it possible to make this more efficient?
        for src_node, values in nl.nodes_src.iteritems():
            # For keeping the order of layers on creation (per take),
            # start with the take-nodes and work through their children.
            if src_node.Checked and type(values['data']) == FBTake:
                # Set it to be the current take.
                FBSystem().CurrentTake = values['data']
                layers_data = list()  # Collect each layer's data in a list.
                for child_node in values['children']:
                    # If we have a checked take, there must be at least one checked layer.
                    if child_node.Checked:
                        src_layer = nl.nodes_src[child_node]['data']
                        # Exclusively select the layer.
                        src_layer.SelectLayer(True, True)
                        # Get AnimationNode:FCurve for all animated components in this layer.
                        components = get_animated_components()
                        curves = dict()
                        for comp in components:
                            curves.update(get_serialized_fcurves(comp))
                        layers_data.append({'layer': src_layer, 'curves': curves})
                        # Reverse order to reconstruct correctly.
                layers_data = layers_data[::-1]
                
                # Now reconstruct the layers on each checked destination take.
                for dst_node, take in nl.nodes_dst.iteritems():
                    if dst_node.Checked:
                        FBSystem().CurrentTake = take
                        for layer_map in layers_data:
                            src_layer = layer_map['layer']
                            take.CreateNewLayer()
                            # Now we need to get a reference on the newly created layer.
                            layerCount = take.GetLayerCount()
                            new_layer = take.GetLayer(layerCount - 1)
                            # Set the new layer's attributes to match the source layer.
                            new_layer.Name = src_layer.Name
                            new_layer.LayerMode = src_layer.LayerMode
                            new_layer.LayerRotationMode = src_layer.LayerRotationMode
                            new_layer.Weight = src_layer.Weight
                            # Set it to be the current layer.
                            new_layer.SelectLayer(True, True)
                            # AnimationNodes are persistent across takes. That means, every component with
                            # animated properties already has AnimationNodes across takes/layers for these, but
                            # the FCurves associated with these properties may not have keys in other takes/layers.
                            for anim_node, curve in layer_map['curves'].iteritems():
                                # Now reconstruct the FCurves on AnimationNodes.
                                deserialize_curve(anim_node.FCurve, curve)
    
    # Callbacks for source panel buttons:
    def is_BaseAnimation_layer(node):
        """Checks whether a tree node connects to a BaseAnimation layer."""
        data = nl.nodes_src[node]
        if type(data['data']) == FBAnimationLayer and data['data'].Name == "BaseAnimation":
            return True
        return False
        
    def src_all_btn_callback(control, event):
        """Checks all nodes."""
        for node in nl.nodes_src.iterkeys():
            node.Checked = True
        main_layout.Refresh()
    
    def src_none_btn_callback(control, event):
        """Unchecks all nodes."""
        for node in nl.nodes_src.iterkeys():
            node.Checked = False
        main_layout.Refresh()
    
    def src_set_take_state():
        """Sets the checked state of the take nodes according to their child nodes state."""
        for node, values in nl.nodes_src.iteritems():
            if type(values['data']) == FBTake:
                some_active = False
                for child in values['children']:
                    some_active = some_active or child.Checked
                node.Checked = some_active
        main_layout.Refresh()
        
    def src_invert_btn_callback(control, event):
        """Inverts checked state on animation layer nodes."""
        for node in nl.nodes_src.iterkeys():
            if type(nl.nodes_src[node]['data']) == FBAnimationLayer:
                node.Checked = not node.Checked
        # Now we need to set the checked state of the take node accordingly.
        src_set_take_state()

    def src_by_name_btn_callback(control, event):
        """Checks/Unchecks nodes that match the pattern."""
        btn, value = FBMessageBoxGetUserValue("Filter by Name", "Enter Substring", "",
                                              FBPopupInputType.kFBPopupString, "Check", "Uncheck", "Cancel",
                                              1, True)
        if btn in [1, 2]:
            for node in nl.nodes_src.iterkeys():
                if value.lower() in node.Name.lower():
                    if btn == 1:
                        node.Checked = True
                    else:
                        node.Checked = False
                    # If we're dealing with a parent take node, set its children.
                    if type(nl.nodes_src[node]['data']) == FBTake:
                        for child in nl.nodes_src[node]['children']:
                            child.Checked = node.Checked
            # If we checked or unchecked all children nodes of a take, also set its state accordingly.
            src_set_take_state()
            main_layout.Refresh()
    
    def src_dup_btn_callback(control, event):
        """Duplicates checked animation layers."""
        first_selected = False  # Did we already have an exclusive selection to add to?
        # Nodes are in an ordered dictionary, so we know a take node is always followed by layers.
        # By iterating in reverse, we can first select the chosen layers and then get a reference to their take.
        for item in reversed(nl.nodes_src.items()):
            # Unpack.
            node, values = item
            if node.Checked:
                # AFTER we selected layers, we must come across their take.
                if type(values['data']) == FBTake:
                    parent_take = values['data']
                    # We first need to set layer to be current, otherwise we face an interesting behavior,
                    # where the layers will be duplicated to the current layer, but only their weight animation.
                    FBSystem().CurrentTake = parent_take
                    # Use the take of the selected layers for duplication.
                    parent_take.DuplicateSelectedLayers()
                    first_selected = False
                    # Let's continue with the layers of another take.
                    continue
                # We need to select the layers and use the take to duplicate selected.
                # To make sure only chosen layers are selected, deselect all others on first selection.
                values['data'].SelectLayer(True, not first_selected)
                first_selected = True
        reload_src_tree()
    
    def src_rename_btn_callback(control, event):
        """Rename checked takes in destination panel."""
        # Only show a popup, if any layer is checked.
        if not any([node.Checked for node in nl.nodes_src]):
            return
        
        btn, new_name = FBMessageBoxGetUserValue("Rename Layers", "Enter name", "",
                                                 FBPopupInputType.kFBPopupString, "Ok", "Cancel", None, 1, True)
        if btn == 1:
            for node, values in nl.nodes_src.iteritems():
                if node.Checked:
                    if not is_BaseAnimation_layer(node) and type(values['data']) != FBTake:
                        layer = values['data']
                        layer.Name = new_name
                        # If the name already existed, the layer is given another name.
                        # Make the node take the actual name the layer was given.
                        node.Name = layer.Name
            # Update the names in the source tree by rebuilding.
            reload_src_tree()
    
    def src_del_btn_callback(control, event):
        """Delete checked Animation layers. Only clear BaseAnimation layer."""
        for node, values in nl.nodes_src.iteritems():
            if node.Checked:
                if type(nl.nodes_src[node]['data']) == FBAnimationLayer:
                    if is_BaseAnimation_layer(node):
                        parent_node = nl.nodes_src[node]['parent']
                        parent_take = nl.nodes_src[parent_node]['data']
                        parent_take.SetCurrentLayer(0)
                        parent_take.ClearAllPropertiesOnCurrentLayer()
                    else:
                        values['data'].FBDelete()
        reload_trees()
    
    def merge_btn_callback(control, event):
        pass
    # Todo: Merge
    # Todo: move up/down

    # Popup menu for muting.
    mute_menu = FBGenericMenu()
    mute_menu.InsertLast("Enable", 10)
    mute_menu.InsertLast("Disable", 100)
    
    def on_mute_click(x, y):
        """Pops up a menu at x,y screen coordinates and returns the chosen state."""
        item = mute_menu.Execute(x, y)
        if item is not None:
            if item.Id == 10:
                return True
            elif item.Id == 100:
                return False
        else:
            return None
        
    def mute_btn_callback(control, event):
        """Set Mute state on checked animation layers."""
        # To launch a popup menu at the mouse, get its position.
        point = _point_t()
        result = windll.user32.GetCursorPos(pointer(point))
        if result:
            # Get which state has been chosen, if any.
            state = on_mute_click(point.x, point.y)
            if state is not None:
                # Set the state on any checked layer.
                for node, values in nl.nodes_src.iteritems():
                    if node.Checked and not is_BaseAnimation_layer(node):
                        if type(values['data']) == FBAnimationLayer:
                            values['data'].Mute = state

    # Popup menu for layer mode.
    mode_menu = FBGenericMenu()
    mode_menu.InsertLast("Additive", 10)
    mode_menu.InsertLast("Override", 11)
    mode_menu.InsertLast("Override-Passthrough", 12)

    def get_layer_mode(x, y):
        """Pops up a menu at x,y screen coordinates and returns the chosen layer mode."""
        item = mode_menu.Execute(x, y)
        if item is not None:
            if item.Id == 10:
                return FBLayerMode.kFBLayerModeAdditive
            elif item.Id == 11:
                return FBLayerMode.kFBLayerModeOverride
            elif item.Id == 12:
                return FBLayerMode.kFBLayerModeOverridePassthrough
        else:
            return None
        
    def mode_btn_callback(control, event):
        """Set layer mode on checked animation layers."""
        # To launch a popup menu at the mouse, get its position.
        point = _point_t()
        result = windll.user32.GetCursorPos(pointer(point))
        if result:
            # Get which mode has been chosen, if any.
            mode = get_layer_mode(point.x, point.y)
            if mode is not None:
                # Set the state on any checked layer.
                for node, values in nl.nodes_src.iteritems():
                    if node.Checked and not is_BaseAnimation_layer(node):
                        if type(values['data']) == FBAnimationLayer:
                            values['data'].LayerMode = mode

    # Popup menu for layer rotation mode.
    rotation_mode_menu = FBGenericMenu()
    rotation_mode_menu.InsertLast("Per Channel", 10)
    rotation_mode_menu.InsertLast("Per Layer", 11)

    def get_layer_rotation_mode(x, y):
        """Pops up a menu at x,y screen coordinates and returns the chosen layer rotation mode."""
        item = rotation_mode_menu.Execute(x, y)
        if item is not None:
            if item.Id == 10:
                return FBLayerRotationMode.kFBLayerRotationModeEulerRotation
            elif item.Id == 11:
                return FBLayerRotationMode.kFBLayerRotationModeQuaternionRotation
        else:
            return None
        
    def rotation_mode_btn_callback(control, event):
        """Set layer rotation mode on checked animation layers."""
        # To launch a popup menu at the mouse, get its position.
        point = _point_t()
        result = windll.user32.GetCursorPos(pointer(point))
        if result:
            # Get which mode has been chosen, if any.
            mode = get_layer_rotation_mode(point.x, point.y)
            if mode is not None:
                # Set the state on any checked layer.
                for node, values in nl.nodes_src.iteritems():
                    if node.Checked and not is_BaseAnimation_layer(node):
                        if type(values['data']) == FBAnimationLayer:
                            values['data'].LayerRotationMode = mode
    
    def weight_btn_callback(control, event):
        """Set the weight of checked animation layers."""
        # Only ask for value, if any layer is checked.
        if any([node.Checked for node in nl.nodes_src]):
            btn, value = FBMessageBoxGetUserValue("Set Layer Weights", "Weight:", 100.0,
                                                  FBPopupInputType.kFBPopupFloat, "Ok", "Cancel",
                                                  None, 1, True)
            if btn == 1:
                if not 0.0 <= value <= 100.0:
                    FBMessageBox("Error", "value must be between\n0.0 and 100.0", "Ok")
                    return
                for node, values in nl.nodes_src.iteritems():
                    if node.Checked:
                        if type(nl.nodes_src[node]['data']) == FBAnimationLayer:
                            if not is_BaseAnimation_layer(node):
                                layer = nl.nodes_src[node]['data']
                                layer.Weight = value
                FBSystem().Scene.Evaluate()

    def tree_src_check_callback(control, event):
        """Set the Checked state of the parent/child nodes."""
        node = event.TreeNode
        # When a take is checked, check all its layers.
        if type(nl.nodes_src[node]['data']) == FBTake:
            for child in nl.nodes_src[node]['children']:
                child.Checked = node.Checked
        # When a layer is checked, also check the take. If no layer is checked, uncheck the take.
        elif type(nl.nodes_src[node]['data']) == FBAnimationLayer:
            parent_take = nl.nodes_src[node]['parent']
            some_active = False
            for child in nl.nodes_src[parent_take]['children']:
                some_active = some_active or child.Checked
            parent_take.Checked = some_active
    
    def src_on_select_callback(control, event):
        """Sets the current take and layer if selected in the source panel."""
        try:  # In case takes/layers have been manually deleted.
            data = nl.nodes_src[event.TreeNode]['data']
            if type(data) == FBTake:
                FBSystem().CurrentTake = data
            else:  # Must be an animation layer.
                parent_node = nl.nodes_src[event.TreeNode]['parent']
                parent_take = nl.nodes_src[parent_node]['data']
                FBSystem().CurrentTake = parent_take
                # Exclusively select the layer.
                data.SelectLayer(True, True)
        except Exception as e:  # UnboundWrapperError
            print "Error:", e
            print "Reloading AnimationLayersManager lists..."
            reload_trees()
        
    def src_tree_changed_callback(control, event):
        """Called when Selection or content changed.
        Renames take/layer if tree node changed its name.
        """
        try:  # When something was deleted and a reload takes place, there's no selected node.
            node = control.SelectedNodes[-1]
            data = nl.nodes_src[node]['data']
            former_name = data.Name
            if node.Name != former_name:
                if not is_BaseAnimation_layer(node):
                    data.Name = node.Name
                # The take/layer might not accept the name (already taken), so do it the other way around, too.
                node.Name = data.Name
                # If it's a take, update the name of  the node in the destination panel.
                if type(data) == FBTake:
                    for dst_node in nl.nodes_dst.iterkeys():
                        if dst_node.Name == former_name:
                            dst_node.Name = node.Name
                            break
        except IndexError:
            pass

    tree_src.OnClickCheck.Add(tree_src_check_callback)
    tree_src.OnSelect.Add(src_on_select_callback)
    tree_src.OnChange.Add(src_tree_changed_callback)
    
    # Callbacks for destination panel buttons:
    def dst_all_btn_callback(control, event):
        """Checks all nodes."""
        for node in nl.nodes_dst.iterkeys():
            node.Checked = True
        main_layout.Refresh()
    
    def dst_none_btn_callback(control, event):
        """Unchecks all nodes."""
        for node in nl.nodes_dst.iterkeys():
            node.Checked = False
        main_layout.Refresh()
    
    def dst_invert_btn_callback(control, event):
        """Invert the Checked state of the nodes."""
        for node in nl.nodes_dst.iterkeys():
            node.Checked = not node.Checked
        main_layout.Refresh()

    # FixMe: "vcgpdm_dyn(10)-lvm(25)_final(306)" e.g. selects all
    def dst_by_name_btn_callback(control, event):
        """Checks/Unchecks nodes that match the pattern."""
        btn, value = FBMessageBoxGetUserValue("Filter by Name", "Enter Substring", "",
                                              FBPopupInputType.kFBPopupString, "Check", "Uncheck", "Cancel",
                                              1, True)
        if btn in [1, 2]:
            for node in nl.nodes_dst.iterkeys():
                if value.lower() in node.Name.lower():
                    if btn == 1:
                        node.Checked = True
                    else:
                        node.Checked = False
            main_layout.Refresh()
    
    def dst_dup_btn_callback(control, event):
        """Copy checked takes in destination panel."""
        for node, take in nl.nodes_dst.iteritems():
            if node.Checked:
                take.CopyTake(take.Name+"-Copy")
        reload_trees()
    
    def dst_rename_btn_callback(control, event):
        """Rename checked takes in destination panel."""
        # Only show a popup, if any take is checked.
        if not any([node.Checked for node in nl.nodes_dst]):
            return
        
        btn, new_name = FBMessageBoxGetUserValue("Rename Takes", "Enter name", "",
                                                 FBPopupInputType.kFBPopupString, "Ok", "Cancel", None, 1, True)
        if btn == 1:
            for node, take in nl.nodes_dst.iteritems():
                if node.Checked:
                    take.Name = new_name
                    # If the name already existed, the layer is given another name.
                    # Make the node take the actual name the take was given.
                    node.Name = take.Name
            # Update the names in the source tree by rebuilding.
            reload_trees()
    
    def dst_del_btn_callback(control, event):
        """Delete checked takes."""
        for node, take in nl.nodes_dst.iteritems():
            if node.Checked:
                take.FBDelete()
        reload_trees()

    def on_start_changed_callback(control, event):
        """When start frame value is changed, make sure the stop value can't go lower."""
        value = control.Value
        nl.start_frame = value
        stop_edit.Min = value + 1
    
    def on_stop_changed_callback(control, event):
        """When stop frame value is changed, make sure the start value can't go higher."""
        value = control.Value
        nl.stop_frame = value
        start_edit.Max = value - 1
        
    def set_framerange_btn_callback(control, event):
        """Set LocalTimeSpan of checked takes."""
        for node, take in nl.nodes_dst.iteritems():
            if node.Checked:
                take.LocalTimeSpan = FBTimeSpan(
                    FBTime(0, 0, 0, int(nl.start_frame), 0),
                    FBTime(0, 0, 0, int(nl.stop_frame), 0),
                )
    
    def on_format_change(control, event):
        """Sets the output format for rendering."""
        nl.render_format = control.Items[control.ItemIndex]
        
    def render_btn_callback(control, event):
        """Will render the checked takes in the destination panel.
        Prompts user for a directory to save the outputs to.
        File names will be generated from the take names.
        """
        # Only show a popup, if any take is checked.
        if any([node.Checked for node in nl.nodes_dst]):
            # Select folder for output files.
            popup = FBFolderPopup()
            popup.Caption = "Select output folder."
            # Set the default path. Start, where the currently opened file is located.
            popup.Path = os.path.dirname(FBApplication().FBXFileName)
            res = popup.Execute()

            # Proceed, if the dialog wasn't canceled.
            if res:
                app_ref = FBApplication()
                output_path = popup.Path
                # Get the file extension
                extension = "." + nl.render_format
                mgr = FBVideoCodecManager()
                # The first time we render a scene, the codec dialog will be available.
                mgr.VideoCodecMode = FBVideoCodecMode.FBVideoCodecAsk  # FBVideoCodecStored
                
                for node, take in nl.nodes_dst.iteritems():
                    if node.Checked:
                        time_span = take.LocalTimeSpan
                        FBSystem().CurrentTake = take
                        # Wrap in try clause, because 'listdir' call can fail.
                        try:
                            options = FBVideoGrabber().GetOptions()
                            # Do we render image sequences or video containers?
                            if nl.render_format not in ["avi", "mov", "swf"]:
                                # How many digits represent the last frame?
                                num_digits = len(str(take.LocalTimeSpan.GetStop().GetFrame()))
                                # Image sequence render output. Create subfolders. Images get frame number.
                                out_file = "{}/{}/{}{}".format(output_path, take.Name, "#"*num_digits, extension)
                            else:
                                out_file = os.path.join(output_path, take.Name + extension)
                            options.OutputFileName = out_file
                            options.TimeSpan = time_span
                            # Only windows supports mov.
                            if nl.render_format == 'mov' and os.name != 'nt':
                                options.BitsPerPixel = FBVideoRenderDepth.FBVideoRender32Bits
                            
                            if not app_ref.FileRender(options):
                                # We encountered an error.
                                btn = FBMessageBox("ERROR", "See terminal\nor Python output.", "Continue", "Cancel",
                                                   None, 1)
                                print "Error rendering take", take.Name
                                print FBVideoGrabber().GetLastErrorMsg()
                                mgr.VideoCodecMode = FBVideoCodecMode.FBVideoCodecAsk
                                if btn == 2:
                                    break
                            else:
                                # The second time a take is rendered, the same settings will be used.
                                mgr.VideoCodecMode = FBVideoCodecMode.FBVideoCodecStored

                        except Exception as e:
                            # Unkown error encountered... Maybe from the 'listdir' call failing...
                            FBMessageBox("ERROR", "Unknown error encountered. Aborting! " + str(e), "OK", None, None)
                            # Print error message from renderer to console. Could also use FBTrace()
                            print FBVideoGrabber().GetLastErrorMsg()

    def dst_tree_on_select_callback(control, event):
        """Sets the selected take to be the current take."""
        # In case takes have been manually deleted, check.
        if type(nl.nodes_dst[event.TreeNode]) == FBTake:
            FBSystem().CurrentTake = nl.nodes_dst[event.TreeNode]
        else:
            print "Error: Take does not exist anymore. Reloading AnimationLayersManager lists..."
            reload_trees()
    
    tree_dst.OnSelect.Add(dst_tree_on_select_callback)
    
    def dst_tree_changed_callback(control, event):
        """Called when Selection or content changed.
        Renames take if tree node changed its name.
        """
        try:  # When something was deleted and a reload takes place, there's no selected node.
            node = control.SelectedNodes[-1]
        except IndexError:
            return
            
        former_name = nl.nodes_dst[node].Name
        if node.Name != former_name:
            nl.nodes_dst[node].Name = node.Name
            # The take might not accept the name (already taken), so do it the other way around, too.
            node.Name = nl.nodes_dst[node].Name
            # Update the name of  the node in the source panel.
            for src_node in nl.nodes_src.iterkeys():
                if src_node.Name == former_name:
                    src_node.Name = node.Name
                    break
            
    tree_dst.OnChange.Add(dst_tree_changed_callback)

    # Callbacks for center panel buttons:
    def transfer_btn_callback(control, event):
        """Calls transfer operation, refreshes the view and evaluates the scene."""
        transfer_anim_layers()
        reload_src_tree()
        FBSystem().Scene.Evaluate()
    
    def reload_btn_callback(control, event):
        """Clears source and destination panels and rebuilds them."""
        reload_trees()
    
    '''*************#
    # Create Layout #
    #*************'''
    x = FBAddRegionParam(0, FBAttachType.kFBAttachLeft, "")
    y = FBAddRegionParam(0, FBAttachType.kFBAttachTop, "")
    w = FBAddRegionParam(0, FBAttachType.kFBAttachRight, "")
    h = FBAddRegionParam(0, FBAttachType.kFBAttachBottom, "")
    
    main_columns = FBHBoxLayout(FBAttachType.kFBAttachLeft)
    main_layout.AddRegion("main", "main", x, y, w, h)
    main_layout.SetControl("main", main_columns)
    
    # First, add the left panel.
    column = FBVBoxLayout()
    main_columns.AddRelative(column)
    # Add a heading for animation layers.
    heading_row = FBHBoxLayout(FBAttachType.kFBAttachLeft)
    heading_row.AddRelative(None)
    label = FBLabel()
    label.Caption = "Animation Layers"
    label.Justify = FBTextJustify.kFBTextJustifyLeft
    label.Style = FBTextStyle.kFBTextStyleBold
    heading_row.Add(label, 120)
    heading_row.AddRelative(None)
    column.AddRelative(heading_row, 0.1)
    
    # create a top button row.
    buttons_row = FBHBoxLayout(FBAttachType.kFBAttachLeft)
    column.AddRelative(buttons_row, 0.1)
    
    btn_callbacks = (("ALL", src_all_btn_callback),
                     ("None", src_none_btn_callback),
                     ("Invert", src_invert_btn_callback),
                     ("By-Name", src_by_name_btn_callback),
                     )
    
    ratio = 1.0 / len(btn_callbacks)
    for label, func in btn_callbacks:
        btn = FBButton()
        btn.Caption = label
        btn.OnClick.Add(func)
        buttons_row.AddRelative(btn, ratio)
    
    column.AddRelative(tree_src)
    
    # Create a bottom button row1.
    buttons_row = FBHBoxLayout(FBAttachType.kFBAttachLeft)
    column.AddRelative(buttons_row, 0.1)
    
    btn_callbacks = (("Duplicate", src_dup_btn_callback),
                     ("Rename", src_rename_btn_callback),
                     ("Delete", src_del_btn_callback),
                     )
    ratio = 1.0 / len(btn_callbacks)
    for label, func in btn_callbacks:
        btn = FBButton()
        btn.Caption = label
        btn.OnClick.Add(func)
        buttons_row.AddRelative(btn, ratio)
        #btn.Enabled = False
    
    # Create a bottom button row2.
    buttons_row = FBHBoxLayout(FBAttachType.kFBAttachLeft)
    column.AddRelative(buttons_row, 0.1)
    column.AddRelative(None, 0.1)  # Placeholder
    
    btn_callbacks = (("Weight", weight_btn_callback),
                     ("Mute", mute_btn_callback),
                     ("Mode", mode_btn_callback),
                     ("Accum.", rotation_mode_btn_callback),
                     #("Merge", merge_btn_callback),
                     )
    ratio = 1.0 / len(btn_callbacks)
    for label, func in btn_callbacks:
        btn = FBButton()
        btn.Caption = label
        btn.OnClick.Add(func)
        buttons_row.AddRelative(btn, ratio)
        #btn.Enabled = False
    
    # Secondly, add the middle panel.
    column = FBVBoxLayout()
    column.AddRelative(None)
    btn = FBButton()
    btn.Caption = "Transfer>>"
    btn.OnClick.Add(transfer_btn_callback)
    column.Add(btn, 70)
    btn = FBButton()
    btn.Caption = "Reload"
    btn.OnClick.Add(reload_btn_callback)
    column.Add(btn, 70)
    column.AddRelative(None)
    main_columns.Add(column, 70)
    
    # Lastly, add the right panel.
    column = FBVBoxLayout()
    main_columns.AddRelative(column)
    # Add a heading for takes.
    heading_row = FBHBoxLayout(FBAttachType.kFBAttachLeft)
    heading_row.AddRelative(None)
    label = FBLabel()
    label.Caption = "Takes"
    label.Justify = FBTextJustify.kFBTextJustifyLeft
    label.Style = FBTextStyle.kFBTextStyleBold
    heading_row.Add(label, 60)
    heading_row.AddRelative(None)
    column.AddRelative(heading_row, 0.1)
    # create a top button row.
    buttons_row = FBHBoxLayout(FBAttachType.kFBAttachLeft)
    column.AddRelative(buttons_row, 0.1)
    
    btn_callbacks = (("ALL", dst_all_btn_callback),
                     ("None", dst_none_btn_callback),
                     ("Invert", dst_invert_btn_callback),
                     ("By-Name", dst_by_name_btn_callback),
                     )
    ratio = 1.0 / len(btn_callbacks)
    for label, func in btn_callbacks:
        btn = FBButton()
        btn.Caption = label
        btn.OnClick.Add(func)
        buttons_row.AddRelative(btn, ratio)
    
    column.AddRelative(tree_dst)
    
    # Create a bottom button row.
    buttons_row = FBHBoxLayout(FBAttachType.kFBAttachLeft)
    column.AddRelative(buttons_row, 0.1)
    
    btn_callbacks = (("Duplicate", dst_dup_btn_callback),
                     ("Rename", dst_rename_btn_callback),
                     ("Delete", dst_del_btn_callback),
                     )
    ratio = 1.0 / len(btn_callbacks)
    for label, func in btn_callbacks:
        btn = FBButton()
        btn.Caption = label
        btn.OnClick.Add(func)
        buttons_row.AddRelative(btn, ratio)

    # Create a bottom button row2.
    buttons_row = FBHBoxLayout(FBAttachType.kFBAttachLeft)
    column.AddRelative(buttons_row, 0.1)
    # Start frame edit.
    label = FBLabel()
    label.Caption = "Start:"
    buttons_row.Add(label, 32)
    start_edit.OnChange.Add(on_start_changed_callback)
    buttons_row.AddRelative(start_edit, 0.2)
    # Stop frame edit.
    label = FBLabel()
    label.Caption = "End:"
    buttons_row.Add(label, 30)
    stop_edit.OnChange.Add(on_stop_changed_callback)
    buttons_row.AddRelative(stop_edit, 0.2)
    # Set start&stop button
    btn = FBButton()
    btn.Caption = "Set"
    btn.OnClick.Add(set_framerange_btn_callback)
    buttons_row.AddRelative(btn, 0.2)

    # Create a bottom button row3.
    buttons_row = FBHBoxLayout(FBAttachType.kFBAttachLeft)
    column.AddRelative(buttons_row, 0.1)
    
    # Render button
    btn = FBButton()
    btn.Caption = "Render"
    btn.OnClick.Add(render_btn_callback)
    buttons_row.AddRelative(btn, 0.5)
    # Render format
    label = FBLabel()
    label.Caption = "Format:"
    buttons_row.Add(label, 45)
    format_list = FBList()
    format_list.Style = FBListStyle.kFBDropDownList
    formats = ["jpg", "tga", "tif", "tiff", "yuv", "swf", "mov", "avi"]
    for format in formats:
        format_list.Items.append(format)
    # Select the default container.
    format_list.Selected(formats.index(nl.render_format), True)
    format_list.OnChange.Add(on_format_change)
    buttons_row.Add(format_list, 50)


tool_name = "Animation Layers Manager"


def createTool():
    """Tool creation will serve as the hub for all other controls."""
    tool = FBCreateUniqueTool(tool_name)
    tool.StartSizeX = 640
    tool.StartSizeY = 480
    populate_tool(tool)
    # Comment this line if you want to put this script in PythonStartup folder.
    ShowTool(tool)


def main():
    # 1- Ensure the tool is created once at the first script execution.
    # 2- When the script is executed again, "Show" the already existing tool
    if tool_name in FBToolList:
        # Each time you execute this script (by dragging in viewer or execute from python console)
        # You will "show" the tool instead of recreating this tool.
        ShowToolByName(tool_name)
    else:
        # The first time this script is executed, it gets created.
        createTool()


# This is actually where the script starts.
# check namespace
if __name__ in ('__main__', '__builtin__'):
    main()

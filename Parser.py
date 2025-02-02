#Cura plug-in to read SVG files as toolpaths.
#Copyright (C) 2020 Ghostkeeper
#This plug-in is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#This plug-in is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.
#You should have received a copy of the GNU Affero General Public License along with this plug-in. If not, see <https://gnu.org/licenses/>.

import copy #Copy nodes for <use> elements.
import cura.Settings.ExtruderManager #To get settings from the active extruder.
import importlib #To import the FreeType library.
import math #Computing curves and such.
import numpy #Transformation matrices.
import os #To find system fonts.
import os.path #To import the FreeType library.
import re #Parsing D attributes of paths, and font paths for Linux.
import subprocess #To find system fonts in Linux.
import sys #To import the FreeType library.
import threading #To find system fonts asynchronously.
import typing
import UM.Logger #To log parse errors and warnings.
import UM.Platform #To select the correct fonts.
import xml.etree.ElementTree #Just typing.

from . import ExtrudeCommand
from . import TravelCommand

#Import FreeType into sys.modules so that the library can reference itself with absolute imports.
this_plugin_path = os.path.dirname(__file__)
freetype_path = os.path.join(this_plugin_path, "freetype", "__init__.py")
spec = importlib.util.spec_from_file_location("freetype", freetype_path)
freetype_module = importlib.util.module_from_spec(spec)
sys.modules["freetype"] = freetype_module
spec.loader.exec_module(freetype_module)
import freetype #Load fonts.
import freetype.ft_enums #Check font weights and italics.

class Parser:
	"""
	Parses an SVG file.
	"""

	_namespace = "{http://www.w3.org/2000/svg}" #Namespace prefix for all SVG elements.
	_xlink_namespace = "{http://www.w3.org/1999/xlink}" #Namespace prefix for XLink references within the document.

	def __init__(self):
		extruder_stack = cura.Settings.ExtruderManager.ExtruderManager.getInstance().getActiveExtruderStack()
		self.resolution = extruder_stack.getProperty("meshfix_maximum_resolution", "value")
		self.machine_width = extruder_stack.getProperty("machine_width", "value")
		self.machine_depth = extruder_stack.getProperty("machine_depth", "value")

		self.viewport_x = 0
		self.viewport_y = 0
		self.viewport_w = self.machine_width
		self.viewport_h = self.machine_depth
		self.image_w = self.machine_width
		self.image_h = self.machine_depth
		self.unit_w = self.image_w / self.viewport_w
		self.unit_h = self.image_h / self.viewport_h

		self.system_fonts = {} #type: typing.Dict[str, typing.List[str]] #Mapping from family name to list of file names.
		self.detect_fonts_thread = threading.Thread(target=self.find_system_fonts)
		self.detect_fonts_thread.start()
		if UM.Platform.Platform.isWindows():
			self.safe_fonts = {
				"serif": "times new roman",
				"sans-serif": "arial",
				"cursive": "monotype corsova",
				"fantasy": "impact",
				"monospace": "courier new",
				"system-ui": "segoe ui"
			}
		elif UM.Platform.Platform.isOSX():
			self.safe_fonts = {
				"serif": "times",
				"sans-serif": "helvetica",
				"cursive": "apple chancery",
				"fantasy": "papyrus",
				"monospace": "courier",
				"system-ui": ".sf ns text"
			}
		elif UM.Platform.Platform.isLinux():
			self.safe_fonts = {}
			for safe_font in {"serif", "sans-serif", "cursive", "fantasy", "monospace", "system-ui"}:
				try:
					output = subprocess.Popen(["fc-match", safe_font], stdout=subprocess.PIPE).communicate(timeout=10)[0].decode("UTF-8")
					fonts = output[output.find(": ") + 2:]
					fonts = fonts.split("\"")
					while not fonts[0].strip():
						fonts = fonts[1:]
					self.safe_fonts[safe_font] = fonts[0] #Use the first non-empty string as the safe font.
				except: #fc-match doesn't exist? Output is wrong?
					UM.Logger.Logger.logException("w", "Unable to query system fonts.")
					continue

		self.dasharray = [] #The current array of dashes to paint the next line segment with.
		self.dasharray_offset = 0 #The current offset to print the next line segment with.
		self.dasharray_length = 0 #The sum of the dasharray.

	def apply_transformation(self, x, y, transformation) -> typing.Tuple[float, float]:
		"""
		Apply a transformation matrix on some coordinates.
		:param x: The X coordinate of the position to transform.
		:param y: The Y coordinate of the position to transform.
		:param transformation: A transformation matrix to transform this
		coordinate by.
		:return: The transformed X and Y coordinates.
		"""
		position = numpy.array(((float(x), float(y), 1)))
		new_position = numpy.matmul(transformation, position)
		return new_position[0], new_position[1]

	def convert_css(self, css) -> typing.Dict[str, str]:
		"""
		Obtains the CSS properties that we can use from a piece of CSS.
		:param css: The piece of CSS to parse.
		:return: A dictionary containing all CSS attributes that we can parse
		that were discovered in the CSS string.
		"""
		is_float = lambda s: re.fullmatch(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s) is not None
		tautology = lambda s: True
		is_list_of_lengths = lambda s: re.fullmatch(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?(cap|ch|em|ex|ic|lh|rem|rlh|vh|vw|vi|vb|vmin|vmax|px|cm|mm|Q|in|pc|pt|%)?[,\s])*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)?(cap|ch|em|ex|ic|lh|rem|rlh|vh|vw|vi|vb|vmin|vmax|px|cm|mm|Q|in|pc|pt|%)?", s) is not None
		is_length = lambda s: re.fullmatch(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?(cap|ch|em|ex|ic|lh|rem|rlh|vh|vw|vi|vb|vmin|vmax|px|cm|mm|Q|in|pc|pt|%)?", s)
		attribute_validate = { #For each supported attribute, a predicate to validate whether it is correctly formed.
			"font-family": tautology,
			"font-weight": is_float,
			"font-size": is_length,
			"font-style": lambda s: s in {"normal", "italic", "oblique", "initial"}, #Don't include "inherit" since we want it to inherit then as if not set.
			"stroke-dasharray": is_list_of_lengths,
			"stroke-dashoffset": is_length,
			"stroke-width": is_length,
			"text-decoration": tautology, #Not going to do any sort of parsing on this one since it has all the colours and that's just way too complex.
			"text-decoration-line": lambda s: all([part in {"none", "overline", "underline", "line-through", "initial"} for part in s.split()]),
			"text-decoration-style": lambda s: s in {"solid", "double", "dotted", "dashed", "wavy", "initial"},
			"text-transform": lambda s: s in {"none", "capitalize", "uppercase", "lowercase", "initial"}, #Don't include "inherit" again.
			"transform": tautology #Not going to do any sort of parsing on this one because all the transformation functions make it very complex.
		}
		result = {}

		pieces = css.split(";")
		for piece in pieces:
			piece = piece.strip()
			for attribute in attribute_validate:
				if piece.startswith(attribute + ":"):
					piece = piece[len(attribute) + 1:]
					piece = piece.strip()
					if attribute_validate[attribute](piece): #Only store the attribute if it has a valid value.
						result[attribute] = piece
					else:
						UM.Logger.Logger.log("w", "Invalid value for CSS attribute {attribute}: {value}".format(attribute=attribute, value=piece))

		return result

	def convert_dasharray(self, dasharray) -> typing.List[float]:
		"""
		Parses a stroke-dasharray property out of CSS.

		The length elements are converted into millimetres for extrusion.

		The result is stored in self.dasharray, to be used with the next drawn
		lines. Also, the total length is computed and stored in
		self.dasharray_length for re-use.
		:param dasharray: A stroke-dasharray property value.
		"""
		dasharray = dasharray.replace(",", " ")
		length_list = dasharray.split()
		self.dasharray = []
		self.dasharray_length = 0
		for length in length_list:
			length_mm = self.convert_length(length)
			if length_mm < 0:
				continue #Invalid. Ignore this one.
			self.dasharray.append(length_mm)
			self.dasharray_length += length_mm
		if len(self.dasharray) % 2 == 1: #Double the sequence so that every segment is the same w.r.t. which is extruded and which is travelled.
			self.dasharray *= 2
			self.dasharray_length *= 2

	def convert_length(self, dimension, vertical=False, parent_size=None) -> float:
		"""
		Converts a CSS dimension to millimetres.

		For pixels, this assumes a resolution of 96 dots per inch.
		:param dimension: A CSS dimension.
		:param vertical: The dimension is a vertical one, so it should be taken
		relative to other vertical dimensions for some units, such as the
		vertical size of the parent if using percentages.
		:param parent_size: The size in millimetres of the element that contains
		the element that we're getting the dimension for. If ``None``, this will
		be set to the printer's width.
		:return: How many millimetres long that dimension is.
		"""
		number = re.match(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", dimension)
		if not number:
			return 0
		number = number.group(0)
		unit = dimension[len(number):].strip().lower()
		number = float(number)

		if unit == "mm":
			return number
		elif unit == "px":
			return number / 96 * 25.4
		elif unit == "cm":
			return number * 10
		elif unit == "q":
			return number / 4
		elif unit == "in":
			return number * 25.4
		elif unit == "pc":
			return number * 12 / 72 * 25.4
		elif unit == "pt":
			return number / 72 * 25.4

		elif unit == "%":
			if parent_size is None:
				if vertical:
					parent_size = self.image_h
				else:
					parent_size = self.image_w
			return number / 100 * parent_size
		elif unit == "vh" or unit == "vb":
			return number / 100 * self.image_w
		elif unit == "vw" or unit == "vi":
			return number / 100 * self.image_h
		elif unit == "vmin":
			return number / 100 * min(self.image_w, self.image_h)
		elif unit == "vmax":
			return number / 100 * max(self.image_w, self.image_h)

		else: #Assume viewport-units.
			if vertical:
				return number * self.unit_h
			else:
				return number * self.unit_w
		#TODO: Implement font-relative sizes.

	def convert_float(self, dictionary, attribute, default: float) -> float:
		"""
		Parses an attribute as float, if possible.

		If impossible or missing, this returns the default.
		:param dictionary: The attributes dictionary to get the attribute from.
		:param attribute: The attribute to get from the dictionary.
		:param default: The default value for this attribute.
		:return: A floating point number that was in the attribute, or the default.
		"""
		try:
			return float(dictionary.get(attribute, default))
		except ValueError: #Not parsable as float.
			return default

	def convert_font_family(self, font_family) -> str:
		"""
		Parses a font-family, converting it to the file name of a single font
		that is installed on the system.
		:param font_family: The font-family property from CSS.
		:return: The file name of a font that is installed on the system that
		most closely approximates the desired font family.
		"""
		fonts = font_family.split(",")
		fonts = [font.strip() for font in fonts]

		self.detect_fonts_thread.join() #All fonts need to be in at this point.

		for font in fonts:
			if font in self.safe_fonts:
				font = self.safe_fonts[font]
			for candidate in self.system_fonts:
				if font.lower() == candidate.lower(): #Case-insensitive matching.
					return candidate
		UM.Logger.Logger.log("w", "Desired fonts not available on the system: {family}".format(family=font_family))
		if self.safe_fonts["serif"] in self.system_fonts:
			return self.safe_fonts["serif"]
		if self.system_fonts:
			return next(iter(self.system_fonts)) #Take an arbitrary font that is available. Running out of options, here!
		return "Noto Sans" #Default font of Cura. Hopefully that gets installed somewhere.

	def convert_points(self, points) -> typing.Generator[typing.Tuple[float, float], None, None]:
		"""
		Parses a points attribute, turning it into a list of coordinate pairs.

		If there is a syntax error, that part of the points will get ignored.
		Other parts might still be included.
		:param points: A series of points.
		:return: A list of x,y pairs.
		"""
		points = points.replace(",", " ")
		while "  " in points:
			points = points.replace("  ", " ")
		points = points.strip()
		points = points.split()
		if len(points) % 2 != 0: #If we have an odd number of points, leave out the last.
			points = points[:-1]

		for x, y in (points[i:i + 2] for i in range(0, len(points), 2)):
			try:
				yield float(x), float(y)
			except ValueError: #Not properly formatted floats.
				continue

	def convert_transform(self, transform) -> numpy.ndarray:
		"""
		Parses a transformation attribute, turning it into a transformation
		matrix.

		If there is a syntax error somewhere in the transformation, that part of
		the transformation gets ignored. Other parts might still be applied.

		3D transformations are not supported.
		:param transform: A series of transformation commands.
		:return: A Numpy array that would apply the transformations indicated
		by the commands. The array is a 2D affine transformation (3x3).
		"""
		transformation = numpy.identity(3)

		transform = transform.replace(")", ") ") #Ensure that every command is separated by spaces, even though func(0.5)fanc(2) is allowed.
		while "  " in transform:
			transform = transform.replace("  ", " ")
		transform = transform.replace(", ", ",") #Don't split on commas.
		transform = transform.replace(" ,", ",")
		commands = transform.split()
		for command in commands:
			command = command.strip()
			if command == "none":
				continue #Ignore.
			if command == "initial":
				transformation = numpy.identity(3)
				continue

			if "(" not in command:
				continue #Invalid: Not a function.
			name_and_value = command.split("(")
			if len(name_and_value) != 2:
				continue #Invalid: More than one opening bracket.
			name, value = name_and_value
			name = name.strip().lower()
			if ")" not in value:
				continue #Invalid: Bracket not closed.
			value = value[:value.find(")")] #Ignore everything after closing bracket. Should be nothing due to splitting on spaces higher.
			values = [float(val) for val in value.replace(",", " ").split() if val]

			if name == "matrix":
				if len(values) != 6:
					continue #Invalid: Needs 6 arguments.
				transformation = numpy.matmul(transformation, numpy.array(((values[0], values[2], values[4]), (values[1], values[3], values[5]), (0, 0, 1))))
			elif name == "translate":
				if len(values) == 1:
					values.append(0)
				if len(values) != 2:
					continue #Invalid: Translate needs at least 1 and at most 2 arguments.
				transformation = numpy.matmul(transformation, numpy.array(((1, 0, values[0]), (0, 1, values[1]), (0, 0, 1))))
			elif name == "translatex":
				if len(values) != 1:
					continue #Invalid: Needs 1 argument.
				transformation = numpy.matmul(transformation, numpy.array(((1, 0, values[0]), (0, 1, 0), (0, 0, 1))))
			elif name == "translatey":
				if len(values) != 1:
					continue #Invalid: Needs 1 argument.
				transformation = numpy.matmul(transformation, numpy.array(((1, 0, 0), (0, 1, values[0]), (0, 0, 1))))
			elif name == "scale":
				if len(values) == 1:
					values.append(values[0]) #Y scale needs to be the same as X scale then.
				if len(values) != 2:
					continue #Invalid: Scale needs at least 1 and at most 2 arguments.
				transformation = numpy.matmul(transformation, numpy.array(((values[0], 0, 0), (0, values[1], 0), (0, 0, 1))))
			elif name == "scalex":
				if len(values) != 1:
					continue #Invalid: Needs 1 argument.
				transformation = numpy.matmul(transformation, numpy.array(((values[0], 0, 0), (0, 1, 0), (0, 0, 1))))
			elif name == "scaley":
				if len(values) != 1:
					continue #Invalid: Needs 1 argument.
				transformation = numpy.matmul(transformation, numpy.array(((1, 0, 0), (0, values[0], 0), (0, 0, 1))))
			elif name == "rotate" or name == "rotatez": #Allow the 3D operation rotateZ as it simply rotates the 2D image in the same way.
				if len(values) == 1:
					values.append(0)
					values.append(0)
				if len(values) != 3:
					continue #Invalid: Rotate needs 1 or 3 arguments.
				transformation = numpy.matmul(transformation, numpy.array(((1, 0, values[1]), (0, 1, values[2]), (0, 0, 1))))
				transformation = numpy.matmul(transformation, numpy.array(((math.cos(values[0] / 180 * math.pi), -math.sin(values[0] / 180 * math.pi), 0), (math.sin(values[0] / 180 * math.pi), math.cos(values[0] / 180 * math.pi), 0), (0, 0, 1))))
				transformation = numpy.matmul(transformation, numpy.array(((1, 0, -values[1]), (0, 1, -values[2]), (0, 0, 1))))
			elif name == "skew":
				if len(values) != 2:
					continue #Invalid: Needs 2 arguments.
				transformation = numpy.matmul(transformation, numpy.array(((1, math.tan(values[0] / 180 * math.pi), 0), (math.tan(values[1] / 180 * math.pi), 1, 0), (0, 0, 1))))
			elif name == "skewx":
				if len(values) != 1:
					continue #Invalid: Needs 1 argument.
				transformation = numpy.matmul(transformation, numpy.array(((1, math.tan(values[0] / 180 * math.pi), 0), (0, 1, 0), (0, 0, 1))))
			elif name == "skewy":
				if len(values) != 1:
					continue #Invalid: Needs 1 argument.
				transformation = numpy.matmul(transformation, numpy.array(((1, 0, 0), (math.tan(values[0] / 180 * math.pi), 1, 0), (0, 0, 1))))
			else:
				continue #Invalid: Unrecognised transformation operation (or 3D).

		return transformation

	def defaults(self, element) -> None:
		"""
		Sets the defaults for some properties on the document root.
		:param element: The document root.
		"""
		extruder_stack = cura.Settings.ExtruderManager.ExtruderManager.getInstance().getActiveExtruderStack()

		if "stroke-width" not in element.attrib:
			element.attrib["stroke-width"] = extruder_stack.getProperty("wall_line_width_0", "value")
		if "transform" not in element.attrib:
			element.attrib["transform"] = ""

	def dereference_uses(self, element, definitions) -> None:
		"""
		Finds all <use> elements and dereferences them.

		This needs to happen recursively (not just with one XPath query) because
		the definitions themselves may again use other definitions.

		If the uses are recursing infinitely, this currently freezes the loading
		thread. TODO: Track the current stack to check for circular references.
		:param element: The scope within a document within which to find uses of
		definitions to replace them.
		:param definitions: The definitions to search through, indexed by their
		IDs.
		"""
		for use in element.findall(self._namespace + "use"): #TODO: This is case-sensitive. The SVG specification says that is correct, but the rest of this implementation is not sensitive.
			link = use.attrib.get(self._xlink_namespace + "href")
			link = use.attrib.get("href", link)
			if link is None:
				UM.Logger.Logger.log("w", "Encountered <use> element without href!")
				continue
			if not link.startswith("#"):
				UM.Logger.Logger.log("w", "SVG document links to {link}, which is outside of this document.".format(link=link))
				#TODO: To support this, we need to:
				#TODO:  - Reference the URL relative to this document.
				#TODO:  - Download the URL if it is not local.
				#TODO:  - Open and parse the document's XML to fetch different definitions.
				#TODO:  - Fetch the correct subelement from the resulting document, for the fragment of the URL.
				continue
			link = link[1:]
			if link not in definitions:
				UM.Logger.Logger.log("w", "Reference to unknown element with ID: {link}".format(link=link))
				continue
			element_copy = copy.deepcopy(definitions[link])
			transform = use.attrib.get("transform", "")
			if transform:
				element_transform = element_copy.attrib.get("transform", "")
				element_copy.attrib["transform"] = transform + " " + element_transform
			x = use.attrib.get("x", "0")
			y = use.attrib.get("y", "0")
			element_transform = element_copy.attrib.get("transform", "")
			element_copy.attrib["transform"] = "translate({x},{y}) ".format(x=x, y=y) + element_transform
			element.append(element_copy)
			element.remove(use)

		for child in element: #Recurse (after dereferencing uses).
			self.dereference_uses(child, definitions)

	def extrude_arc(self, start_x, start_y, rx, ry, rotation, large_arc, sweep_flag, end_x, end_y, line_width, transformation) -> typing.Generator[ExtrudeCommand.ExtrudeCommand, None, None]:
		"""
		Yields points of an elliptical arc spaced at the required resolution.

		The parameters of the arc include the starting X,Y coordinates as well
		as all of the parameters of an A command in an SVG path.
		:param start_x: The X coordinate where the arc starts.
		:param start_y: The Y coordinate where the arc starts.
		:param rx: The X radius of the ellipse to follow.
		:param ry: The Y radius of the ellipse to follow.
		:param rotation: The rotation angle of the ellipse in radians.
		:param large_arc: Whether to take the longest way around or the shortest
		side of the ellipse.
		:param sweep_flag: On which side of the path the centre of the ellipse
		will be.
		:param end_x: The X coordinate of the final position to end up at.
		:param end_y: The Y coordinate of the final position to end up at.
		:param line_width: The width of the lines to extrude with.
		:param transformation: A transformation matrix to apply to the arc.
		:return: A sequence of extrude commands that follow the arc.
		"""
		if start_x == end_x and start_y == end_y: #Nothing to draw.
			return
		rx = abs(rx)
		ry = abs(ry)
		if rx == 0 or ry == 0: #Invalid radius. Skip this arc.
			yield from self.extrude_line(start_x, start_y, end_x, end_y, line_width, transformation)
			return
		start_tx, start_ty = self.apply_transformation(start_x, start_y, transformation)
		end_tx, end_ty = self.apply_transformation(end_x, end_y, transformation)
		if (end_tx - start_tx) * (end_tx - start_tx) + (end_ty - start_ty) * (end_ty - start_ty) <= self.resolution * self.resolution: #Too small to fit with higher resolution.
			yield from self.extrude_line(start_x, start_y, end_x, end_y, line_width, transformation)
			return

		#Implementation of https://www.w3.org/TR/SVG/implnote.html#ArcImplementationNotes to find centre of ellipse.
		#Based off: https://stackoverflow.com/a/12329083
		sin_rotation = math.sin(rotation / 180 * math.pi)
		cos_rotation = math.cos(rotation / 180 * math.pi)
		x1 = cos_rotation * (start_x - end_x) / 2.0 + sin_rotation * (start_y - end_y) / 2.0
		y1 = cos_rotation * (start_y - end_y) / 2.0 + sin_rotation * (start_x - end_x) / 2.0
		lambda_multiplier = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
		if lambda_multiplier > 1:
			rx *= math.sqrt(lambda_multiplier)
			ry *= math.sqrt(lambda_multiplier)
		sum_squares = rx * y1 * rx * y1 + ry * x1 * ry * x1
		coefficient = math.sqrt(abs((rx * ry * rx * ry - sum_squares) / sum_squares))
		if large_arc == sweep_flag:
			coefficient = -coefficient
		cx_original = coefficient * rx * y1 / ry
		cy_original = -coefficient * ry * x1 / rx
		cx = cos_rotation * cx_original - sin_rotation * cy_original + (start_x + end_x) / 2.0
		cy = sin_rotation * cx_original + cos_rotation * cy_original + (start_y + end_y) / 2.0
		xcr_start = (x1 - cx_original) / rx
		xcr_end = (x1 + cx_original) / rx
		ycr_start = (y1 - cy_original) / ry
		ycr_end = (y1 + cy_original) / ry

		mod = math.sqrt(xcr_start * xcr_start + ycr_start * ycr_start)
		start_angle = math.acos(xcr_start / mod)
		if ycr_start < 0:
			start_angle = -start_angle
		dot = -xcr_start * xcr_end - ycr_start * ycr_end
		mod = math.sqrt((xcr_start * xcr_start + ycr_start * ycr_start) * (xcr_end * xcr_end + ycr_end * ycr_end))
		delta_angle = math.acos(dot / mod)
		if xcr_start * ycr_end - ycr_start * xcr_end < 0:
			delta_angle = -delta_angle
		delta_angle %= math.pi * 2
		if not sweep_flag:
			delta_angle -= math.pi * 2
		end_angle = start_angle + delta_angle

		#Use Newton's method to find segments of the required length along the ellipsis, basically using binary search.
		current_x = start_x
		current_y = start_y
		current_tx, current_ty = self.apply_transformation(current_x, current_y, transformation)
		while (current_tx - end_tx) * (current_tx - end_tx) + (current_ty - end_ty) * (current_ty - end_ty) > self.resolution * self.resolution: #While further than the resolution, make new points.
			lower_angle = start_angle #Regardless of in which direction the delta_angle goes.
			upper_angle = end_angle
			current_error = self.resolution
			new_x = current_x
			new_y = current_y
			new_angle = lower_angle
			while abs(current_error) > 0.001: #Continue until 1 micron error.
				new_angle = (lower_angle + upper_angle) / 2
				if new_angle == lower_angle or new_angle == upper_angle: #Get out of infinite loop if we're ever stuck.
					break
				new_x = math.cos(new_angle) * rx
				new_x_temp = new_x
				new_y = math.sin(new_angle) * ry
				new_x = cos_rotation * new_x - sin_rotation * new_y
				new_y = sin_rotation * new_x_temp + cos_rotation * new_y
				new_x += cx
				new_y += cy
				new_tx, new_ty = self.apply_transformation(new_x, new_y, transformation)
				current_tx, current_ty = self.apply_transformation(current_x, current_y, transformation)
				current_step = math.sqrt((new_tx - current_tx) * (new_tx - current_tx) + (new_ty - current_ty) * (new_ty - current_ty))
				current_error = current_step - self.resolution
				if current_error > 0: #Step is too far.
					upper_angle = new_angle
				else: #Step is not far enough.
					lower_angle = new_angle
			yield from self.extrude_line(current_x, current_y, new_x, new_y, line_width, transformation)
			current_x = new_x
			current_y = new_y
			current_tx, current_ty = self.apply_transformation(current_x, current_y, transformation)
			start_angle = new_angle
		yield from self.extrude_line(current_x, current_y, end_x, end_y, line_width, transformation)

	def extrude_cubic(self, start_x, start_y, handle1_x, handle1_y, handle2_x, handle2_y, end_x, end_y, line_width, transformation) -> typing.Generator[ExtrudeCommand.ExtrudeCommand, None, None]:
		"""
		Yields points of a cubic (Bézier) arc spaced at the required resolution.

		A cubic arc takes three adjacent line segments (from start to handle1,
		from handle1 to handle2 and from handle2 to end) and varies a parameter
		p. Along the first and second line segment, a point is drawn at the
		ratio p between the segment's start and end. A line segment is drawn
		between these sliding points, and another point is made at a ratio of p
		along this line segment. That point follows a quadratic curve. Then the
		same thing is done for the second and third line segments, creating
		another point that follows a quadratic curve. Between these two points,
		a last line segment is drawn and a final point is drawn at a ratio of p
		along this line segment. As p varies from 0 to 1, this final point moves
		along the cubic curve.
		:param start_x: The X coordinate where the curve starts.
		:param start_y: The Y coordinate where the curve starts.
		:param handle1_x: The X coordinate of the first handle.
		:param handle1_y: The Y coordinate of the first handle.
		:param handle2_x: The X coordinate of the second handle.
		:param handle2_y: The Y coordinate of the second handle.
		:param end_x: The X coordinate where the curve ends.
		:param end_y: The Y coordinate where the curve ends.
		:param line_width: The width of the line to extrude.
		:param transformation: A transformation matrix to apply to the curve.
		:return: A sequence of commands necessary to print this curve.
		"""
		current_x = start_x
		current_y = start_y
		current_tx, current_ty = self.apply_transformation(current_x, current_y, transformation)
		end_tx, end_ty = self.apply_transformation(end_x, end_y, transformation)
		p_min = 0
		p_max = 1
		while (current_tx - end_tx) * (current_tx - end_tx) + (current_ty - end_ty) * (current_ty - end_ty) > self.resolution * self.resolution: #Keep stepping until we're closer than one step from our goal.
			#Find the value for p that gets us exactly one step away (after transformation).
			new_x = current_x
			new_y = current_y
			new_error = self.resolution
			new_p = p_min
			while abs(new_error) > 0.001: #Continue until 1 micron error.
				#Graduate towards smaller steps first.
				#This is necessary because the cubic curve can loop back on itself and the halfway point may be beyond the intersection.
				#If we were to try a high p value that happens to fall very close to the starting point due to the loop,
				#we would think that the p is not high enough even though it is actually too high and thus skip the loop.
				#With cubic curves, that looping point can never occur at 1/4 of the curve or earlier, so try 1/4 of the parameter.
				new_p = (p_min * 3 + p_max) / 4
				if new_p == p_min or new_p == p_max: #Get out of infinite loop if we're ever stuck.
					break
				#Calculate the three points on the linear segments.
				linear1_x = start_x + new_p * (handle1_x - start_x)
				linear1_y = start_y + new_p * (handle1_y - start_y)
				linear2_x = handle1_x + new_p * (handle2_x - handle1_x)
				linear2_y = handle1_y + new_p * (handle2_y - handle1_y)
				linear3_x = handle2_x + new_p * (end_x - handle2_x)
				linear3_y = handle2_y + new_p * (end_y - handle2_y)
				#Calculate the two points on the quadratic curves.
				quadratic1_x = linear1_x + new_p * (linear2_x - linear1_x)
				quadratic1_y = linear1_y + new_p * (linear2_y - linear1_y)
				quadratic2_x = linear2_x + new_p * (linear3_x - linear2_x)
				quadratic2_y = linear2_y + new_p * (linear3_y - linear2_y)
				#Interpolate on the line between those points to get the final cubic position for new_p.
				new_x = quadratic1_x + new_p * (quadratic2_x - quadratic1_x)
				new_y = quadratic1_y + new_p * (quadratic2_y - quadratic1_y)
				new_tx, new_ty = self.apply_transformation(new_x, new_y, transformation)
				new_error = math.sqrt((new_tx - current_tx) * (new_tx - current_tx) + (new_ty - current_ty) * (new_ty - current_ty)) - self.resolution
				if new_error > 0: #Step is too far.
					p_max = new_p
				else: #Step is not far enough.
					p_min = new_p
			yield from self.extrude_line(current_x, current_y, new_x, new_y, line_width, transformation)
			current_x = new_x
			current_y = new_y
			current_tx, current_ty = self.apply_transformation(current_x, current_y, transformation)
			p_min = new_p
			p_max = 1
		yield from self.extrude_line(current_x, current_y, end_x, end_y, line_width, transformation) #And the last step to end exactly on our goal.

	def extrude_line(self, start_x, start_y, end_x, end_y, line_width, transformation) -> typing.Generator[ExtrudeCommand.ExtrudeCommand, None, None]:
		"""
		Extrude a line towards a destination.
		:param start_x: The X position to start the line at.
		:param start_y: The Y position to start the line at.
		:param end_x: The X position of the destination.
		:param end_y: The Y position of the destination.
		:param line_width: The line width of the line to draw.
		:param transformation: Any transformation matrix to apply to the line.
		:return: A sequence of commands necessary to print the line.
		"""
		end_tx, end_ty = self.apply_transformation(end_x, end_y, transformation)
		if self.dasharray:
			start_tx, start_ty = self.apply_transformation(start_x, start_y, transformation)
			dx = end_tx - start_tx
			dy = end_ty - start_ty
			line_length = math.sqrt(dx * dx + dy * dy)

			while self.dasharray_offset < 0:
				self.dasharray_offset += self.dasharray_length

			#Find the position in the dasharray that we're at now.
			cumulative_sum = 0
			current_index = 0
			while cumulative_sum + self.dasharray[current_index] < self.dasharray_offset:
				cumulative_sum += self.dasharray[current_index]
				current_index = (current_index + 1) % len(self.dasharray)
			partial_segment = self.dasharray_offset - cumulative_sum #How far along the first segment we'll start.
			is_extruding = current_index % 2 == 0

			position = 0 #Position along the line segment.
			direction_x = dx / line_length
			direction_y = dy / line_length
			while position < line_length:
				position += self.dasharray[current_index]
				if partial_segment > 0:
					position -= partial_segment
					partial_segment = 0
				position = max(min(position, line_length), 0)

				x = start_tx + direction_x * position - self.viewport_x * self.unit_w
				y = start_ty + direction_y * position - self.viewport_y * self.unit_h
				if is_extruding:
					yield ExtrudeCommand.ExtrudeCommand(x, y, line_width)
				else:
					yield TravelCommand.TravelCommand(x, y)
				current_index = (current_index + 1) % len(self.dasharray)
				is_extruding = not is_extruding

			self.dasharray_offset += line_length
		yield ExtrudeCommand.ExtrudeCommand(end_tx - self.viewport_x * self.unit_w, end_ty - self.viewport_y * self.unit_h, line_width)

	def extrude_quadratic(self, start_x, start_y, handle_x, handle_y, end_x, end_y, line_width, transformation) -> typing.Generator[ExtrudeCommand.ExtrudeCommand, None, None]:
		"""
		Yields points of a quadratic arc spaced at the required resolution.

		A quadratic arc takes two adjacent line segments (from start to handle
		and from handle to end) and varies a parameter p. Along each of these
		two line segments, a point is drawn at the ratio p between the segment's
		start and end. A line segment is drawn between these sliding points, and
		another point is made at a ratio of p along this line segment. As p
		varies from 0 to 1, this last point moves along the quadratic curve.
		:param start_x: The X coordinate where the curve starts.
		:param start_y: The Y coordinate where the curve starts.
		:param handle_x: The X coordinate of the handle halfway along the curve.
		:param handle_y: The Y coordinate of the handle halfway along the curve.
		:param end_x: The X coordinate where the curve ends.
		:param end_y: The Y coordinate where the curve ends.
		:param line_width: The width of the line to extrude.
		:param transformation: A transformation matrix to apply to the curve.
		:return: A sequence of commands necessary to print this curve.
		"""
		end_tx, end_ty = self.apply_transformation(end_x, end_y, transformation)
		#First check if handle lies exactly between start and end. If so, we just draw one line from start to finish.
		if start_x == end_x:
			if handle_x == start_x and (start_y <= handle_y <= end_y or start_y >= handle_y >= end_y):
				yield from self.extrude_line(start_x, start_y, end_x, end_y, line_width, transformation)
				return
		elif start_y == end_y:
			if handle_y == start_y and (start_x <= handle_x <= end_x or start_x >= handle_x >= end_x):
				yield from self.extrude_line(start_x, start_y, end_x, end_y, line_width, transformation)
				return
		else:
			slope_deviation = (handle_x - start_x) / (end_x - start_x) - (handle_y - start_y) / (end_y - start_y)
			if abs(slope_deviation) == 0:
				if start_x <= handle_x <= end_x or start_x >= handle_x >= end_x:
					yield from self.extrude_line(start_x, start_y, end_x, end_y, line_width, transformation)
					return

		current_x = start_x
		current_y = start_y
		current_tx, current_ty = self.apply_transformation(current_x, current_y, transformation)
		p_min = 0
		p_max = 1
		while (current_tx - end_tx) * (current_tx - end_tx) + (current_ty - end_ty) * (current_ty - end_ty) > self.resolution * self.resolution: #Keep stepping until we're closer than one step from our goal.
			#Find the value for p that gets us exactly one step away (after transformation).
			new_x = current_x
			new_y = current_y
			new_error = self.resolution
			new_p = p_min
			while abs(new_error) > 0.001: #Continue until 1 micron error.
				new_p = (p_min + p_max) / 2
				if new_p == p_min or new_p == p_max: #Get out of infinite loop if we're ever stuck.
					break
				#Calculate the two points on the linear segments.
				linear1_x = start_x + new_p * (handle_x - start_x)
				linear1_y = start_y + new_p * (handle_y - start_y)
				linear2_x = handle_x + new_p * (end_x - handle_x)
				linear2_y = handle_y + new_p * (end_y - handle_y)
				#Interpolate on the line between those points to get the final quadratic position for new_p.
				new_x = linear1_x + new_p * (linear2_x - linear1_x)
				new_y = linear1_y + new_p * (linear2_y - linear1_y)
				new_tx, new_ty = self.apply_transformation(new_x, new_y, transformation)
				new_error = math.sqrt((new_tx - current_tx) * (new_tx - current_tx) + (new_ty - current_ty) * (new_ty - current_ty)) - self.resolution
				if new_error > 0: #Step is too far.
					p_max = new_p
				else: #Step is not far enough.
					p_min = new_p
			yield from self.extrude_line(current_x, current_y, new_x, new_y, line_width, transformation)
			current_x = new_x
			current_y = new_y
			current_tx, current_ty = self.apply_transformation(current_x, current_y, transformation)
			p_min = new_p
			p_max = 1
		yield from self.extrude_line(current_x, current_y, end_x, end_y, line_width, transformation) #And the last step to end exactly on our goal.

	def find_definitions(self, element) -> typing.Dict[str, xml.etree.ElementTree.Element]:
		"""
		Finds all element definitions in an element tree.
		:param element: An element whose descendants we must register.
		:return: A dictionary mapping element IDs to their elements.
		"""
		definitions = {}
		for definition in element.findall(".//*[@id]"):
			definitions[definition.attrib["id"]] = definition
		return definitions

	def find_system_fonts(self) -> None:
		"""
		Finds all the fonts installed on the system, arranged by their font
		family name.

		This takes a while. It will scan through all the font files in the font
		directories of your system. It is advisable to run this in a thread.

		The result gets put in self.system_fonts.
		"""
		if UM.Platform.Platform.isWindows():
			font_paths = {os.path.join(os.getenv("WINDIR"), "Fonts")}
		else:
			font_paths = set()
			chkfontpath_executable = "/usr/sbin/chkfontpath"
			if os.path.isfile(chkfontpath_executable):
				chkfontpath_stdout = os.popen(chkfontpath_executable).readlines()
				path_match = re.compile(r"\d+: (.+)")
				for line in chkfontpath_stdout:
					result = path_match.match(line)
					if result:
						font_paths.add(result.group(1))
			else:
				font_paths = {
					os.path.expanduser("~/Library/Fonts"),
					os.path.expanduser("~/.fonts"),
					"/Library/Fonts",
					"/Network/Library/Fonts",
					"/System/Library/Fonts",
					"/System Folder/Fonts",
					"/usr/X11R6/lib/X11/fonts/TTF",
					"/usr/lib/openoffice/share/fonts/truetype",
					"/usr/share/fonts",
					"/usr/local/share/fonts"
				}

		for font_path in font_paths:
			if not os.path.isdir(font_path):
				continue #This one doesn't exist.
			for root, _, filenames in os.walk(font_path):
				for filename in filenames:
					filename = os.path.join(root, filename)
					try:
						face = freetype.Face(filename)
					except freetype.FT_Exception: #Unrecognised file format. Lots of fonts are pixel-based and FreeType can't read those.
						continue
					try:
						family_name = face.family_name.decode("utf-8").lower()
					except: #Family name is not UTF-8?
						continue
					if family_name not in self.system_fonts:
						self.system_fonts[family_name] = []
					self.system_fonts[family_name].append(filename)

		UM.Logger.Logger.log("d", "Completed scan for system fonts.")

	def inheritance(self, element) -> None:
		"""
		Pass inherited properties of elements down through the node tree.

		Some properties, if not specified by child elements, should be taken
		from parent elements.

		This also parses the style property and turns it into the corresponding
		attributes.
		:param element: The parent element whose attributes have to be applied
		to all descendants.
		"""
		css = {} #Dictionary of all the attributes that we'll track.

		#Special case CSS entries that have an SVG attribute.
		if "transform" in element.attrib:
			css["transform"] = element.attrib["transform"]
		if "stroke-width" in element.attrib:
			try:
				css["stroke-width"] = str(float(element.attrib["stroke-width"]))
			except ValueError: #Not parseable as float.
				pass
		if "stroke-dasharray" in element.attrib:
			css["stroke-dasharray"] = element.attrib["stroke-dasharray"]
		if "stroke-dashoffset" in element.attrib:
			css["stroke-dashoffset"] = element.attrib["stroke-dashoffset"]

		#Find <style> subelements and add them to our CSS.
		for child in element:
			if child.tag.lower() == self._namespace + "style":
				style_css = self.convert_css(child.text)
				css.update(style_css) #Merge into main CSS file, overwriting attributes if necessary.

		#CSS in the 'style' attribute overrides <style> element and separate attributes.
		if "style" in element.attrib:
			style_css = self.convert_css(element.attrib["style"])
			css.update(style_css)
			del element.attrib["style"]

		#Put all CSS attributes in the attrib dict, even if they are not normally available in SVG. It'll be easier to parse there if we keep it separated.
		tracked_css = { #For each property, also their defaults.
			"font-family": "serif",
			"font-size": "12pt",
			"font-style": "normal",
			"font-weight": "400",
			"stroke-dasharray": "",
			"stroke-width": "0.35mm",
			"text-decoration": "",
			"text-decoration-line": "",
			"text-decoration-style": "solid",
			"text-transform": "none",
			"transform": ""
		}
		for attribute in tracked_css:
			if attribute in element.attrib and attribute not in css:
				css[attribute] = element.attrib[attribute] #CSS overrides the separate attributes, but we still want to inherit the separate attributes.
			element.attrib[attribute] = css.get(attribute, tracked_css[attribute])

		#Pass CSS on to children.
		for child in element:
			for attribute in css:
				if attribute == "transform": #Transform is special because it adds on to the children's transforms.
					if "transform" not in child.attrib:
						child.attrib["transform"] = ""
					child.attrib["transform"] = css["transform"] + " " + child.attrib["transform"]
				else:
					if attribute not in child.attrib:
						child.attrib[attribute] = css[attribute]
			self.inheritance(child)

	def parse(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses an XML element and returns the paths required to print said
		element.

		This function delegates the parsing to the correct specialist function.
		:param element: The element to print.
		:return: A sequence of commands necessary to print this element.
		"""
		if not element.tag.lower().startswith(self._namespace):
			return #Ignore elements not in the SVG namespace.
		tag = element.tag[len(self._namespace):].lower()
		if tag == "circle":
			yield from self.parse_circle(element)
		elif tag == "defs":
			return #Ignore defs.
		elif tag == "ellipse":
			yield from self.parse_ellipse(element)
		elif tag == "g":
			yield from self.parse_g(element)
		elif tag == "line":
			yield from self.parse_line(element)
		elif tag == "path":
			yield from self.parse_path(element)
		elif tag == "polygon":
			yield from self.parse_polygon(element)
		elif tag == "polyline":
			yield from self.parse_polyline(element)
		elif tag == "rect":
			yield from self.parse_rect(element)
		elif tag == "svg":
			yield from self.parse_svg(element)
		elif tag == "switch":
			yield from self.parse_switch(element)
		elif tag == "text":
			yield from self.parse_text(element)
		else:
			UM.Logger.Logger.log("w", "Unknown element {element_tag}.".format(element_tag=tag))
			#SVG specifies that you should ignore any unknown elements.

	def parse_circle(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the Circle element.
		:param element: The Circle element.
		:return: A sequence of commands necessary to print this element.
		"""
		cx = self.convert_length(element.attrib.get("cx", "0"))
		cy = self.convert_length(element.attrib.get("cy", "0"), vertical=True)
		r = self.convert_length(element.attrib.get("r", "0"))
		if r == 0:
			return #Circles without radius don't exist here.
		line_width = self.convert_length(element.attrib.get("stroke-width", "0.35mm"))
		transformation = self.convert_transform(element.attrib.get("transform", ""))
		self.convert_dasharray(element.attrib.get("stroke-dasharray", ""))
		self.dasharray_offset = self.convert_length(element.attrib.get("stroke-dashoffset", "0"))

		yield from self.travel(cx + r, cy, transformation)
		yield from self.extrude_arc(cx + r, cy, r, r, 0, False, False, cx, cy - r, line_width, transformation)
		yield from self.extrude_arc(cx, cy - r, r, r, 0, False, False, cx - r, cy, line_width, transformation)
		yield from self.extrude_arc(cx - r, cy, r, r, 0, False, False, cx, cy + r, line_width, transformation)
		yield from self.extrude_arc(cx, cy + r, r, r, 0, False, False, cx + r, cy, line_width, transformation)

	def parse_ellipse(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the Ellipse element.
		:param element: The Ellipse element.
		:return: A sequence of commands necessary to print this element.
		"""
		cx = self.convert_length(element.attrib.get("cx", "0"))
		cy = self.convert_length(element.attrib.get("cy", "0"), vertical=True)
		rx = self.convert_length(element.attrib.get("rx", "0"))
		if rx == 0:
			return #Ellipses without radius don't exist here.
		ry = self.convert_length(element.attrib.get("ry", "0"), vertical=True)
		if ry == 0:
			return
		line_width = self.convert_length(element.attrib.get("stroke-width", "0.35mm"))
		transformation = self.convert_transform(element.attrib.get("transform", ""))
		self.convert_dasharray(element.attrib.get("stroke-dasharray", ""))
		self.dasharray_offset = self.convert_length(element.attrib.get("stroke-dashoffset", "0"))

		yield from self.travel(cx + rx, cy, transformation)
		yield from self.extrude_arc(cx + rx, cy, rx, ry, 0, False, False, cx, cy - ry, line_width, transformation)
		yield from self.extrude_arc(cx, cy - ry, rx, ry, 0, False, False, cx - rx, cy, line_width, transformation)
		yield from self.extrude_arc(cx - rx, cy, rx, ry, 0, False, False, cx, cy + ry, line_width, transformation)
		yield from self.extrude_arc(cx, cy + ry, rx, ry, 0, False, False, cx + rx, cy, line_width, transformation)

	def parse_g(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the G element.

		This element simply passes on its attributes to its children.
		:param element: The G element.
		:return: A sequence of commands necessary to print this element.
		"""
		for child in element:
			yield from self.parse(child)

	def parse_line(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the Line element.

		This element creates a line from one coordinate to another.
		:param element: The Line element.
		:return: A sequence of commands necessary to print this element.
		"""
		line_width = self.convert_length(element.attrib.get("stroke-width", "0.35mm"))
		transformation = self.convert_transform(element.attrib.get("transform", ""))
		self.convert_dasharray(element.attrib.get("stroke-dasharray", ""))
		self.dasharray_offset = self.convert_length(element.attrib.get("stroke-dashoffset", "0"))
		x1 = self.convert_length(element.attrib.get("x1", "0"))
		y1 = self.convert_length(element.attrib.get("y1", "0"), vertical=True)
		x2 = self.convert_length(element.attrib.get("x2", "0"))
		y2 = self.convert_length(element.attrib.get("y2", "0"), vertical=True)

		yield from self.travel(x1, y1, transformation)
		yield from self.extrude_line(x1, y1, x2, y2, line_width, transformation)

	def parse_path(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the Path element.

		This element creates arbitrary curves. It is as powerful as all the
		other elements put together!
		:param element: The Path element.
		:return: A sequence of commands necessary to print this element.
		"""
		line_width = self.convert_length(element.attrib.get("stroke-width", "0.35mm"))
		transformation = self.convert_transform(element.attrib.get("transform", ""))
		self.convert_dasharray(element.attrib.get("stroke-dasharray", ""))
		dasharray_offset = self.convert_length(element.attrib.get("stroke-dashoffset", "0"))
		d = element.attrib.get("d", "")
		x = 0 #Starting position.
		y = 0

		d = d.replace(",", " ")
		d = d.strip()

		start_x = 0 #Track movement command for Z command to return to beginning.
		start_y = 0
		previous_quadratic_x = 0 #Track the previous curve handle of Q commands for the T command.
		previous_quadratic_y = 0 #This is always absolute!
		previous_cubic_x = 0 #And for the second cubic handle of C commands for the S command, too.
		previous_cubic_y = 0

		#Since all commands in the D attribute are single-character letters, we can split the thing on alpha characters and process each command separately.
		commands = re.findall(r"[A-DF-Za-df-z][^A-DF-Za-df-z]*", d)
		for command in commands:
			command = command.strip()
			command_name = command[0]
			command = command[1:]
			parameters = [float(match) for match in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", command)] #Ignore parameters that are not properly formatted floats.

			#Process M and m commands first since they can have some of their parameters apply to different commands.
			if command_name == "M": #Move.
				if len(parameters) < 2:
					continue #Not enough parameters to the M command. Skip it.
				x = parameters[0] * self.unit_w
				y = parameters[1] * self.unit_h
				yield from self.travel(x, y, transformation)
				self.dasharray_offset = dasharray_offset
				if len(parameters) >= 2:
					command_name = "L" #The next parameters are interpreted as being lines.
					parameters = parameters[2:]
				start_x = x #Start a new path.
				start_y = y
			if command_name == "m": #Move relatively.
				if len(parameters) < 2:
					continue #Not enough parameters to the m command. Skip it.
				x += parameters[0] * self.unit_w
				y += parameters[1] * self.unit_h
				yield from self.travel(x, y, transformation)
				self.dasharray_offset = dasharray_offset
				if len(parameters) >= 2:
					command_name = "l" #The next parameters are interpreted as being relative lines.
					parameters = parameters[2:]
				start_x = x #Start a new path.
				start_y = y

			if command_name == "A": #Elliptical arc.
				while len(parameters) >= 7:
					if (parameters[3] != 0 and parameters[3] != 1) or (parameters[4] != 0 and parameters[4] != 1):
						parameters = parameters[7:]
						continue #The two flag parameters need to be 0 or 1, otherwise we won't be able to interpret them.
					parameters[3] = parameters[3] != 0 #Convert to boolean.
					parameters[4] = parameters[4] != 0
					yield from self.extrude_arc(start_x=x, start_y=y,
					                 rx=parameters[0] * self.unit_w, ry=parameters[1] * self.unit_h,
					                 rotation=parameters[2],
					                 large_arc=parameters[3], sweep_flag=parameters[4],
					                 end_x=parameters[5] * self.unit_w, end_y=parameters[6] * self.unit_h, line_width=line_width, transformation=transformation)
					x = parameters[5] * self.unit_w
					y = parameters[6] * self.unit_h
					parameters = parameters[7:]
			elif command_name == "a": #Elliptical arc to relative position.
				while len(parameters) >= 7:
					if (parameters[3] != 0 and parameters[3] != 1) or (parameters[4] != 0 and parameters[4] != 1):
						parameters = parameters[7:]
						continue #The two flag parameters need to be 0 or 1, otherwise we won't be able to interpret them.
					parameters[3] = parameters[3] != 0 #Convert to boolean.
					parameters[4] = parameters[4] != 0
					yield from self.extrude_arc(start_x=x, start_y=y,
					                 rx=parameters[0] * self.unit_w, ry=parameters[1] * self.unit_h,
					                 rotation=parameters[2],
					                 large_arc=parameters[3], sweep_flag=parameters[4],
					                 end_x=x + parameters[5] * self.unit_w, end_y=y + parameters[6] * self.unit_h, line_width=line_width, transformation=transformation)
					x += parameters[5] * self.unit_w
					y += parameters[6] * self.unit_h
					parameters = parameters[7:]
			elif command_name == "C": #Cubic curve (Bézier).
				while len(parameters) >= 6:
					previous_cubic_x = parameters[2] * self.unit_w
					previous_cubic_y = parameters[3] * self.unit_h
					yield from self.extrude_cubic(start_x=x, start_y=y,
					                              handle1_x=parameters[0] * self.unit_w, handle1_y=parameters[1] * self.unit_h,
					                              handle2_x=previous_cubic_x, handle2_y=previous_cubic_y,
					                              end_x=parameters[4] * self.unit_w, end_y=parameters[5] * self.unit_h,
					                              line_width=line_width, transformation=transformation)
					x = parameters[4] * self.unit_w
					y = parameters[5] * self.unit_h
					parameters = parameters[6:]
			elif command_name == "c": #Relative cubic curve (Bézier).
				while len(parameters) >= 6:
					previous_cubic_x = x + parameters[2] * self.unit_w
					previous_cubic_y = y + parameters[3] * self.unit_h
					yield from self.extrude_cubic(start_x=x, start_y=y,
					                              handle1_x=x + parameters[0] * self.unit_w, handle1_y=y + parameters[1] * self.unit_h,
					                              handle2_x=previous_cubic_x, handle2_y=previous_cubic_y,
					                              end_x=x + parameters[4] * self.unit_w, end_y=y + parameters[5] * self.unit_h,
					                              line_width=line_width, transformation=transformation)
					x += parameters[4] * self.unit_w
					y += parameters[5] * self.unit_h
					parameters = parameters[6:]
			elif command_name == "H": #Horizontal line.
				while len(parameters) >= 1:
					yield from self.extrude_line(x, y, parameters[0] * self.unit_w, y, line_width, transformation)
					x = parameters[0] * self.unit_w
					parameters = parameters[1:]
			elif command_name == "h": #Relative horizontal line.
				while len(parameters) >= 1:
					yield from self.extrude_line(x, y, x + parameters[0] * self.unit_w, y, line_width, transformation)
					x += parameters[0] * self.unit_w
					parameters = parameters[1:]
			elif command_name == "L": #Line.
				while len(parameters) >= 2:
					yield from self.extrude_line(x, y, parameters[0] * self.unit_w, parameters[1] * self.unit_h, line_width, transformation)
					x = parameters[0] * self.unit_w
					y = parameters[1] * self.unit_h
					parameters = parameters[2:]
			elif command_name == "l": #Relative line.
				while len(parameters) >= 2:
					yield from self.extrude_line(x, y, x + parameters[0] * self.unit_w, y + parameters[1] * self.unit_h, line_width, transformation)
					x += parameters[0] * self.unit_w
					y += parameters[1] * self.unit_h
					parameters = parameters[2:]
			elif command_name == "Q": #Quadratic curve.
				while len(parameters) >= 4:
					previous_quadratic_x = parameters[0] * self.unit_w
					previous_quadratic_y = parameters[1] * self.unit_h
					yield from self.extrude_quadratic(start_x=x, start_y=y,
					                                  handle_x=previous_quadratic_x, handle_y=previous_quadratic_y,
					                                  end_x=parameters[2] * self.unit_w, end_y=parameters[3] * self.unit_h,
					                                  line_width=line_width, transformation=transformation)
					x = parameters[2] * self.unit_w
					y = parameters[3] * self.unit_h
					parameters = parameters[4:]
			elif command_name == "q": #Relative quadratic curve.
				while len(parameters) >= 4:
					previous_quadratic_x = x + parameters[0] * self.unit_w
					previous_quadratic_y = y + parameters[1] * self.unit_h
					yield from self.extrude_quadratic(start_x=x, start_y=y,
					                                  handle_x=previous_quadratic_x, handle_y=previous_quadratic_y,
					                                  end_x=x + parameters[2] * self.unit_w, end_y=y + parameters[3] * self.unit_h,
					                                  line_width=line_width, transformation=transformation)
					x += parameters[2] * self.unit_w
					y += parameters[3] * self.unit_h
					parameters = parameters[4:]
			elif command_name == "S": #Smooth cubic curve (Bézier).
				while len(parameters) >= 4:
					#Mirror the handle around the current position.
					handle1_x = x + (x - previous_cubic_x)
					handle1_y = y + (y - previous_cubic_y)
					previous_cubic_x = parameters[0] * self.unit_w #For the next curve, store the coordinates of the second handle.
					previous_cubic_y = parameters[1] * self.unit_h
					yield from self.extrude_cubic(start_x=x, start_y=y,
					                              handle1_x=handle1_x, handle1_y=handle1_y,
					                              handle2_x=previous_cubic_x, handle2_y=previous_cubic_y,
					                              end_x=parameters[2] * self.unit_w, end_y=parameters[3] * self.unit_h,
					                              line_width=line_width, transformation=transformation)
					x = parameters[2] * self.unit_w
					y = parameters[3] * self.unit_h
					parameters = parameters[4:]
			elif command_name == "s": #Relative smooth cubic curve (Bézier).
				while len(parameters) >= 4:
					#Mirror the handle around the current position.
					handle1_x = x + (x - previous_cubic_x)
					handle1_y = y + (y - previous_cubic_y)
					previous_cubic_x = x + parameters[0] * self.unit_w #For the next curve, store the coordinates of the second handle.
					previous_cubic_y = y + parameters[1] * self.unit_h
					yield from self.extrude_cubic(start_x=x, start_y=y,
					                              handle1_x=handle1_x, handle1_y=handle1_y,
					                              handle2_x=previous_cubic_x, handle2_y=previous_cubic_y,
					                              end_x=x + parameters[2] * self.unit_w, end_y=y + parameters[3] * self.unit_h,
					                              line_width=line_width, transformation=transformation)
					x += parameters[2] * self.unit_w
					y += parameters[3] * self.unit_h
					parameters = parameters[4:]
			elif command_name == "T": #Smooth quadratic curve.
				while len(parameters) >= 2:
					#Mirror the handle around the current position.
					previous_quadratic_x = x + (x - previous_quadratic_x)
					previous_quadratic_y = y + (y - previous_quadratic_y)
					yield from self.extrude_quadratic(start_x=x, start_y=y,
					                                  handle_x=previous_quadratic_x, handle_y=previous_quadratic_y,
					                                  end_x=parameters[0] * self.unit_w, end_y=parameters[1] * self.unit_h,
					                                  line_width=line_width, transformation=transformation)
					x = parameters[0] * self.unit_w
					y = parameters[1] * self.unit_h
					parameters = parameters[2:]
			elif command_name == "t": #Relative smooth quadratic curve.
				while len(parameters) >= 2:
					#Mirror the handle around the current position.
					previous_quadratic_x = x + (x - previous_quadratic_x)
					previous_quadratic_y = y + (y - previous_quadratic_y)
					yield from self.extrude_quadratic(start_x=x, start_y=y,
					                                  handle_x=previous_quadratic_x, handle_y=previous_quadratic_y,
					                                  end_x=x + parameters[0] * self.unit_w, end_y=y + parameters[1] * self.unit_h,
					                                  line_width=line_width, transformation=transformation)
					x += parameters[0] * self.unit_w
					y += parameters[1] * self.unit_h
					parameters = parameters[2:]
			elif command_name == "V": #Vertical line.
				while len(parameters) >= 1:
					yield from self.extrude_line(x, y, x, parameters[0] * self.unit_h, line_width, transformation)
					y = parameters[0] * self.unit_h
					parameters = parameters[1:]
			elif command_name == "v": #Relative vertical line.
				while len(parameters) >= 1:
					yield from self.extrude_line(x, y, x, y + parameters[0] * self.unit_h, line_width, transformation)
					y += parameters[0] * self.unit_h
					parameters = parameters[1:]
			elif command_name == "Z" or command_name == "z":
				yield from self.extrude_line(x, y, start_x, start_y, line_width, transformation)
				x = start_x
				y = start_y
			else: #Unrecognised command, or M or m which we processed separately.
				pass

			if command_name != "Q" and command_name != "q" and command_name != "T" and command_name != "t":
				previous_quadratic_x = x
				previous_quadratic_y = y
			if command_name != "C" and command_name != "c" and command_name != "S" and command_name != "s":
				previous_cubic_x = x
				previous_cubic_y = y

	def parse_polygon(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the Polygon element.

		This element lists a number of vertices past which to travel, and the
		polygon is closed at the end.
		:param element: The Polygon element.
		:return: A sequence of commands necessary to print this element.
		"""
		line_width = self.convert_length(element.attrib.get("stroke-width", "0.35mm"))
		transformation = self.convert_transform(element.attrib.get("transform", ""))
		self.convert_dasharray(element.attrib.get("stroke-dasharray", ""))
		self.dasharray_offset = self.convert_length(element.attrib.get("stroke-dashoffset", "0"))

		first_x = None #Save these in order to get back to the starting coordinates. And to use a travel command.
		first_y = None
		prev_x = None #Save in order to provide a starting position to the extrude_line method.
		prev_y = None
		for x, y in self.convert_points(element.attrib.get("points", "")):
			x *= self.unit_w
			y *= self.unit_h
			if first_x is None or first_y is None or prev_x is None or prev_y is None:
				first_x = x
				first_y = y
				yield from self.travel(x, y, transformation)
			else:
				yield from self.extrude_line(prev_x, prev_y, x, y, line_width, transformation)
			prev_x = x
			prev_y = y
		if first_x is not None and first_y is not None: #Close the polygon.
			yield from self.extrude_line(prev_x, prev_y, first_x, first_y, line_width, transformation)

	def parse_polyline(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the Polyline element.

		This element lists a number of vertices past which to travel. The line
		is not closed into a loop, contrary to the Polygon element.
		:param element: The Polyline element.
		:return: A sequence of commands necessary to print this element.
		"""
		line_width = self.convert_length(element.attrib.get("stroke-width", "0.35mm"))
		transformation = self.convert_transform(element.attrib.get("transform", ""))
		self.convert_dasharray(element.attrib.get("stroke-dasharray", ""))
		self.dasharray_offset = self.convert_length(element.attrib.get("stroke-dashoffset", "0"))

		is_first = True #We must use a travel command for the first coordinate pair.
		prev_x = None
		prev_y = None
		for x, y in self.convert_points(element.attrib.get("points", "")):
			x *= self.unit_w
			y *= self.unit_h
			if is_first:
				yield from self.travel(x, y, transformation)
				is_first = False
			else:
				yield from self.extrude_line(prev_x, prev_y, x, y, line_width, transformation)
			prev_x = x
			prev_y = y

	def parse_rect(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the Rect element.
		:param element: The Rect element.
		:return: A sequence of commands necessary to print this element.
		"""
		x = self.convert_length(element.attrib.get("x", "0"))
		y = self.convert_length(element.attrib.get("y", "0"), vertical=True)
		rx = self.convert_length(element.attrib.get("rx", "0"))
		ry = self.convert_length(element.attrib.get("ry", "0"), vertical=True)
		width = self.convert_length(element.attrib.get("width", "0"))
		height = self.convert_length(element.attrib.get("height", "0"), vertical=True)
		line_width = self.convert_length(element.attrib.get("stroke-width", "0.35mm"))
		transformation = self.convert_transform(element.attrib.get("transform", ""))
		self.convert_dasharray(element.attrib.get("stroke-dasharray", ""))
		self.dasharray_offset = self.convert_length(element.attrib.get("stroke-dashoffset", "0"))

		if width == 0 or height == 0:
			return #No surface, no print!
		rx = min(rx, width / 2) #Limit rounded corners to half the rectangle.
		ry = min(ry, height / 2)
		yield from self.travel(x + rx, y, transformation)
		yield from self.extrude_line(x + rx, y, x + width - rx, y, line_width, transformation)
		yield from self.extrude_arc(x + width - rx, y, rx, ry, 0, False, True, x + width, y + ry, line_width, transformation)
		yield from self.extrude_line(x + width, y + ry, x + width, y + height - ry, line_width, transformation)
		yield from self.extrude_arc(x + width, y + height - ry, rx, ry, 0, False, True, x + width - rx, y + height, line_width, transformation)
		yield from self.extrude_line(x + width - rx, y + height, x + rx, y + height, line_width, transformation)
		yield from self.extrude_arc(x + rx, y + height, rx, ry, 0, False, True, x, y + height - ry, line_width, transformation)
		yield from self.extrude_line(x, y + height - ry, x, y + ry, line_width, transformation)
		yield from self.extrude_arc(x, y + ry, rx, ry, 0, False, True, x + rx, y, line_width, transformation)

	def parse_svg(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the SVG element, which basically concatenates all commands put
		forth by its children.
		:param element: The SVG element.
		:return: A sequence of commands necessary to print this element.
		"""
		if "viewBox" in element.attrib:
			parts = element.attrib["viewBox"].split()
			if len(parts) == 4:
				try:
					self.viewport_x = float(parts[0])
					self.viewport_y = float(parts[1])
					self.viewport_w = float(parts[2])
					self.viewport_h = float(parts[3])
				except ValueError: #Not valid floats.
					pass
		self.image_w = self.convert_length(element.attrib.get("width", "100%"))
		self.image_h = self.convert_length(element.attrib.get("height", "100%"), vertical=True)
		self.unit_w = self.image_w / self.viewport_w
		self.unit_h = self.image_h / self.viewport_h

		for child in element:
			yield from self.parse(child)

	def parse_switch(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the Switch element, which can decide to show or not show its
		child elements based on if features are implemented or not.
		:param element: The Switch element.
		:return: A sequence of commands necessary to print this element.
		"""
		#For some of these features we're actually lying, since we support most of what the feature entails so for 99% of the files that use them it should be fine.
		supported_features = {
			"", #If there is no required feature, this will appear in the set.
			"http://www.w3.org/TR/SVG11/feature#SVG", #Since v1.0.0.
			"http://www.w3.org/TR/SVG11/feature#SVGDOM", #Since v1.0.0.
			"http://www.w3.org/TR/SVG11/feature#SVG-static", #Since v1.0.0.
			"http://www.w3.org/TR/SVG11/feature#SVGDOM-static", #Since v1.0.0.
			"http://www.w3.org/TR/SVG11/feature#CoreAttribute", #Since v1.1.0. Actually unsupported: xml:base (since embedding SVGs is not implemented yet).
			"http://www.w3.org/TR/SVG11/feature#Structure", #Since v1.0.0. Actually unsupported: <symbol>.
			"http://www.w3.org/TR/SVG11/feature#BasicStructure", #Since v1.1.0. Actually unsupported: <title>.
			"http://www.w3.org/TR/SVG11/feature#ConditionalProcessing", #Since v1.0.0. Actually unsupported: requiredExtensions and systemLanguage.
			"http://www.w3.org/TR/SVG11/feature#Style" #Since v1.0.0.
			"http://www.w3.org/TR/SVG11/feature#Shape" #Since v1.0.0.
			"http://www.w3.org/TR/SVG11/feature#BasicText" #Since v1.1.0.
			"http://www.w3.org/TR/SVG11/feature#PaintAttribute" #Since v1.1.0.
			"http://www.w3.org/TR/SVG11/feature#BasicPaintAttribute" #Since v1.1.0.
			"http://www.w3.org/TR/SVG11/feature#ColorProfile" #Doesn't apply to g-code.
			"http://www.w3.org/TR/SVG11/feature#Gradient" #Doesn't apply to g-code.
		}
		required_features = element.attrib.get("requiredFeatures", "")
		required_features = {feature.strip() for feature in required_features.split(",")}

		if required_features - supported_features:
			return #Not all required features are supported.
		else:
			for child in element:
				yield from self.parse(child)

	def parse_text(self, element) -> typing.Generator[typing.Union[TravelCommand.TravelCommand, ExtrudeCommand.ExtrudeCommand], None, None]:
		"""
		Parses the Text element, which writes a bit of text.
		:param element: The Text element.
		:return: A sequence of commands necessary to write the text.
		"""
		x = self.convert_length(element.attrib.get("x", "0"))
		y = self.convert_length(element.attrib.get("y", "0"), vertical=True)
		x += self.convert_length(element.attrib.get("dx", "0"))
		y += self.convert_length(element.attrib.get("dy", "0"), vertical=True)
		rotate = self.convert_float(element.attrib, "rotate", 0)
		length_adjust = element.attrib.get("lengthAdjust", "spacing")
		text_length = self.convert_length(element.attrib.get("textLength", "0"))
		line_width = self.convert_length(element.attrib.get("stroke-width", "0.35mm"))
		transformation = self.convert_transform(element.attrib.get("transform", ""))
		self.convert_dasharray(element.attrib.get("stroke-dasharray", ""))
		dasharray_offset = self.convert_length(element.attrib.get("stroke-dashoffset", "0"))

		text_transform = element.attrib.get("text-transform", "none")
		text = " ".join(element.text.split()) #Change all whitespace into spaces.
		if text_transform == "capitalize":
			text = " ".join((word.capitalize() for word in text.split()))
		elif text_transform == "uppercase":
			text = text.upper()
		elif text_transform == "lowercase":
			text = text.lower()

		character_stretch_x = 1

		#Select the correct font based on name, italics and boldness.
		font_name = self.convert_font_family(element.attrib.get("font-family", "serif").lower())
		font_style = element.attrib.get("font-style", "normal")
		font_weight = self.convert_float(element.attrib, "font-weight", 400)
		is_italic = font_style == "italic"
		is_oblique = font_style == "oblique"
		is_bold = font_weight >= 550 #Freetype doesn't support getting the font's weight or adjusting it.
		face = freetype.Face(self.system_fonts[font_name][0])
		best_attributes_satisfied = set()
		for candidate in self.system_fonts[font_name]:
			candidate_face = freetype.Face(candidate)
			candidate_flags = candidate_face.style_flags
			attributes_satisfied = set()
			if is_italic and (candidate_flags & freetype.ft_enums.FT_STYLE_FLAGS["FT_STYLE_FLAG_ITALIC"]) > 0:
				attributes_satisfied.add("italic")
			elif not is_italic and (candidate_flags & freetype.ft_enums.FT_STYLE_FLAGS["FT_STYLE_FLAG_ITALIC"]) == 0:
				attributes_satisfied.add("italic")
			if is_bold and (candidate_flags & freetype.ft_enums.FT_STYLE_FLAGS["FT_STYLE_FLAG_BOLD"]) > 0:
				attributes_satisfied.add("bold")
			elif not is_bold and (candidate_flags & freetype.ft_enums.FT_STYLE_FLAGS["FT_STYLE_FLAG_BOLD"]) == 0:
				attributes_satisfied.add("bold")

			if len(attributes_satisfied) > len(best_attributes_satisfied):
				face = candidate_face
				best_attributes_satisfied = attributes_satisfied
		if is_italic and "italic" not in best_attributes_satisfied:
			is_oblique = True #Artificial italics. Only applied if italics are required but not available, not the other way around because it'll be unknown how italic they are so it can't really be undone.
		if "bold" not in best_attributes_satisfied:
			if is_bold:
				character_stretch_x = font_weight / 400
			else:
				character_stretch_x = 400 / font_weight

		font_size = self.convert_length(element.attrib.get("font-size", "12pt"))
		face.set_char_size(0, int(round(font_size / 25.4 * 72 * 64)), 362, 362) #This DPI of 362 seems to be the magic number to get the font size correct, but I don't know why.
		ascent = face.ascender / 64 / 72 * 25.4
		height = face.height / 64 / 72 * 25.4

		char_x = 0 #Position of this character within the text element.
		char_y = 0
		previous_char = 0 #To get correct kerning.
		for index, character in enumerate(text):
			per_character_transform = numpy.matmul(transformation, self.convert_transform("translate({x}, {y})".format(x=x + char_x, y=y + char_y)))
			per_character_transform = numpy.matmul(per_character_transform, self.convert_transform("scalex({scalex})".format(scalex=character_stretch_x)))
			if is_oblique:
				per_character_transform = numpy.matmul(per_character_transform, self.convert_transform("translate(0, -{ascent})".format(ascent=ascent)))
				per_character_transform = numpy.matmul(per_character_transform, self.convert_transform("skewx(-10)"))
				per_character_transform = numpy.matmul(per_character_transform, self.convert_transform("translate(0, {ascent})".format(ascent=ascent)))
			per_character_transform = numpy.matmul(per_character_transform, self.convert_transform("rotate({rotation})".format(rotation=rotate)))
			per_character_transform = numpy.matmul(per_character_transform, self.convert_transform("translate(-{x}, -{y})".format(x=x + char_x, y=y + char_y)))
			face.load_char(character)
			outline = face.glyph.outline
			start = 0
			for contour_index in range(len(outline.contours)):
				self.dasharray_offset = dasharray_offset
				end = outline.contours[contour_index]
				if end < start:
					continue
				points = outline.points[start:end + 1]
				points.append(points[0]) #Close the polygon.
				for point_idx in range(len(points)): #Convert coordinates to mm.
					points[point_idx] = (points[point_idx][0] / 64.0 / 96.0 * 25.4, -points[point_idx][1] / 64.0 / 96.0 * 25.4)
				tags = outline.tags[start:end + 1]
				tags.append(tags[0])

				current_x, current_y = points[0][0], points[0][1]
				yield from self.travel(x + char_x + current_x, y + char_y + current_y, per_character_transform) #Move to first segment.

				current_curve = [] #Between every on-curve point we'll draw a curve. These are the cubic handles of the curve.
				for point_index in range(1, len(points)):
					if tags[point_index] & 0b1: #First bit is set, so this point is on the curve and finishes the segment.
						current_curve.append(points[point_index])
						#Actually extrude the curve.
						while len(current_curve) > 0:
							if len(current_curve) == 1: #Just enough left for a straight line, whatever the flags of the last point are.
								yield from self.extrude_line(x + char_x + current_x, y + char_y + current_y, x + char_x + current_curve[0][0], y + char_y + current_curve[0][1], line_width, per_character_transform)
								current_x, current_y = current_curve[0]
								current_curve = []
							elif len(current_curve) == 2: #Just enough left for a quadratic curve, even though the curve specified cubic. Shouldn't happen if the font was correctly formed.
								yield from self.extrude_quadratic(x + char_x + current_x, y + char_y + current_y,
								                                  x + char_x + current_curve[0][0], y + char_y + current_curve[0][1],
								                                  x + char_x + current_curve[1][0], y + char_y + current_curve[1][1],
								                                  line_width, per_character_transform)
								current_x = current_curve[1][0]
								current_y = current_curve[1][1]
								current_curve = []
							elif len(current_curve) == 3: #Just enough left for a single cubic curve.
								yield from self.extrude_cubic(x + char_x + current_x, y + char_y + current_y,
								                              x + char_x + current_curve[0][0], y + char_y + current_curve[0][1],
								                              x + char_x + current_curve[1][0], y + char_y + current_curve[1][1],
								                              x + char_x + current_curve[2][0], y + char_y + current_curve[2][1],
								                              line_width, per_character_transform)
								current_x = current_curve[2][0]
								current_y = current_curve[2][1]
								current_curve = []
							else: #Multiple curves with implied midway points.
								end_x = (current_curve[1][0] + current_curve[2][0]) / 2
								end_y = (current_curve[1][1] + current_curve[2][1]) / 2
								yield from self.extrude_cubic(x + char_x + current_x, y + char_y + current_y,
								                              x + char_x + current_curve[0][0], y + char_y + current_curve[0][1],
								                              x + char_x + current_curve[1][0], y + char_y + current_curve[1][1],
								                              x + char_x + end_x, y + char_y + end_y,
								                              line_width, per_character_transform)
								current_x = end_x
								current_y = end_y
								current_curve = current_curve[2:]
						continue
					if tags[point_index] & 0b10 or point_index >= len(points) - 1: #If the second bit is set, this is a cubic curve control point. If it's the last point, convert to normal linear point.
						current_curve.append(points[point_index])
					else: #If second bit is unset, this is a quadratic curve which we can convert to a cubic curve.
						control = points[point_index]
						if tags[point_index + 1] & 0b1:
							next_point = points[point_index + 1]
						else:
							next_point = ((control[0] + points[point_index + 1][0]) / 2, (control[1] + points[point_index + 1][1]) / 2)
						if tags[point_index - 1] & 0b1:
							previous_point = points[point_index - 1]
						else:
							previous_point = ((control[0] + points[point_index - 1][0]) / 2, (control[1] + points[point_index - 1][1]) / 2)
						current_curve.append((previous_point[0] + 2.0 / 3.0 * (control[0] - previous_point[0]), previous_point[1] + 2.0 / 3.0 * (control[1] - previous_point[1]))) #2/3 towards the one control point.
						current_curve.append((next_point[0] + 2.0 / 3.0 * (control[0] - next_point[0]), next_point[1] + 2.0 / 3.0 * (control[1] - next_point[1])))

				start = end + 1

			kerning = face.get_kerning(previous_char, character)
			char_x += (face.glyph.advance.x + kerning.x) / 64.0 / 96.0 * 25.4
			char_y -= (face.glyph.advance.y + kerning.y) / 64.0 / 96.0 * 25.4
			previous_char = character

		total_width = char_x
		decoration_lines = element.attrib.get("text-decoration-line", "")
		decoration_lines = decoration_lines.split()
		decoration_style = element.attrib.get("text-decoration-style", "solid")
		decorations = element.attrib.get("text-decoration", "")
		decorations = decorations.split()
		for decoration in decorations: #Allow any order. It seems to get used interchangibly.
			if decoration in {"overline", "underline", "line-through", "none"}:
				decoration_lines.append(decoration)
			if decoration in {"solid", "double", "wavy", "dotted", "dashed"}:
				decoration_style = decoration
		for decoration_line in decoration_lines:
			if decoration_line == "underline":
				line_y = y - face.underline_position / 64.0 / 96.0 * 25.4
			elif decoration_line == "overline":
				line_y = y - height
			elif decoration_line == "line-through":
				line_y = y - ascent / 2
			else:
				continue

			if decoration_style in {"solid", "double", "wavy"}:
				self.dasharray = []
				self.dasharray_offset = 0
				self.dasharray_length = 0
			elif decoration_style == "dotted":
				self.dasharray = [line_width, line_width]
				self.dasharray_offset = 0
				self.dasharray_length = line_width * 2
			elif decoration_style == "dashed":
				self.dasharray = [line_width * 3, line_width * 3]
				self.dasharray_offset = 0
				self.dasharray_length = line_width * 6

			if decoration_style in {"solid", "dotted", "dashed", "double"}:
				yield from self.travel(x, line_y, transformation)
				yield from self.extrude_line(x, line_y, x + total_width, line_y, line_width, transformation)
			if decoration_style == "double": #Draw an extra line underneath the first one for the double line.
				yield from self.travel(x, line_y + line_width * 2, transformation)
				yield from self.extrude_line(x, line_y + line_width * 2, x + total_width, line_y + line_width * 2, line_width, transformation)
			if decoration_style == "wavy": #Instead of the previous lines, draw the waves.
				amplitude = ascent / 16
				yield from self.travel(x, line_y + amplitude, transformation)
				wave_x = x
				while wave_x < x + total_width:
					yield from self.extrude_quadratic(wave_x, line_y + amplitude, min(wave_x + amplitude, wave_x + total_width), line_y + amplitude * 2, min(wave_x + amplitude * 2, wave_x + total_width), line_y + amplitude, line_width, transformation)
					wave_x += amplitude * 2
					if wave_x < x + total_width:
						yield from self.extrude_quadratic(wave_x, line_y + amplitude, min(wave_x + amplitude, wave_x + total_width), line_y, min(wave_x + amplitude * 2, wave_x + total_width), line_y + amplitude, line_width, transformation)
						wave_x += amplitude * 2

	def travel(self, end_x, end_y, transformation) -> typing.Generator[TravelCommand.TravelCommand, None, None]:
		"""
		Yields a travel move to the specified destination.
		:param end_x: The X coordinate of the position to travel to.
		:param end_y: The Y coordinate of the position to travel to.
		:param transformation: A transformation matrix to apply to the move.
		:return: A sequence of commands necessary to make the travel move.
		"""
		end_tx, end_ty = self.apply_transformation(end_x, end_y, transformation)
		yield TravelCommand.TravelCommand(x=end_tx - self.viewport_x * self.unit_w, y=end_ty - self.viewport_y * self.unit_h)
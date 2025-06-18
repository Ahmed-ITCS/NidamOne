import re
from ast import literal_eval
from functools import lru_cache
from types import BuiltinFunctionType
from typing import TYPE_CHECKING, Any, TypeAlias

import sqlparse
from pypika.queries import QueryBuilder, Table
from pypika.terms import Term

import frappe
from frappe import _
from frappe.database.operator_map import NESTED_SET_OPERATORS, OPERATOR_MAP
from frappe.database.schema import SPECIAL_CHAR_PATTERN
from frappe.database.utils import DefaultOrderBy, FilterValue, convert_to_value, get_doctype_name
from frappe.model import get_permitted_fields
from frappe.query_builder import Criterion, Field, Order, functions
from frappe.query_builder.functions import Function, SqlFunctions
from frappe.query_builder.utils import PseudoColumnMapper
from frappe.utils.data import MARIADB_SPECIFIC_COMMENT

if TYPE_CHECKING:
	from frappe.query_builder import DocType

TAB_PATTERN = re.compile("^tab")
WORDS_PATTERN = re.compile(r"\w+")
BRACKETS_PATTERN = re.compile(r"\(.*?\)|$")
SQL_FUNCTIONS = tuple(f"{sql_function.value}(" for sql_function in SqlFunctions)  # ) <- ignore this comment.
COMMA_PATTERN = re.compile(r",\s*(?![^()]*\))")

# less restrictive version of frappe.core.doctype.doctype.doctype.START_WITH_LETTERS_PATTERN
# to allow table names like __Auth
TABLE_NAME_PATTERN = re.compile(r"^[\w -]*$", flags=re.ASCII)

# Pattern to validate field names in SELECT:
# Allows: name, `name`, name as alias, `name` as alias, `table`.`name`, `table`.`name` as alias, table.name, table.name as alias
ALLOWED_FIELD_PATTERN = re.compile(r"^(?:`?\w+`?\.)?(`?\w+`?|\w+)(?:\s+as\s+\w+)?$", flags=re.ASCII)

# Pattern to validate basic SQL function call syntax: word(...) [as alias]
FUNCTION_CALL_PATTERN = re.compile(r"^\w+\(.*\)(?:\s+as\s+\w+)?$", flags=re.IGNORECASE | re.ASCII)

# Pattern to validate field names used in various SQL clauses (WHERE, GROUP BY, ORDER BY):
# Allows simple field names, backticked names, and table-qualified names (e.g., name, `name`, `table`.`name`, table.name)
# Does NOT allow aliases ('as alias') or functions.
ALLOWED_SQL_FIELD_PATTERN = re.compile(r"^(?:`?\w+`?\.)?(`?\w+`?|\w+)$", flags=re.ASCII)

# Regex to parse field names:
# Group 1: Optional table name (e.g., `tabDocType` or tabDocType)
# Group 2: Field name (e.g., `field` or field)
FIELD_PARSE_REGEX = re.compile(r"^(?:[`\"]?(tab\w+)[`\"]?\.)?[`\"]?(\w+)[`\"]?$")


class Engine:
	def get_query(
		self,
		table: str | Table,
		fields: str | list | tuple | None = None,
		filters: dict[str, FilterValue] | FilterValue | list[list | FilterValue] | None = None,
		order_by: str | None = None,
		group_by: str | None = None,
		limit: int | None = None,
		offset: int | None = None,
		distinct: bool = False,
		for_update: bool = False,
		update: bool = False,
		into: bool = False,
		delete: bool = False,
		*,
		validate_filters: bool = False,
		skip_locked: bool = False,
		wait: bool = True,
		ignore_permissions: bool = True,
		user: str | None = None,
		parent_doctype: str | None = None,
	) -> QueryBuilder:
		qb = frappe.local.qb
		db_type = frappe.local.db.db_type

		self.is_mariadb = db_type == "mariadb"
		self.is_postgres = db_type == "postgres"
		self.is_sqlite = db_type == "sqlite"
		self.validate_filters = validate_filters
		self.user = user or frappe.session.user
		self.parent_doctype = parent_doctype
		self.apply_permissions = not ignore_permissions

		if isinstance(table, Table):
			self.table = table
			self.doctype = get_doctype_name(table.get_sql())
		else:
			self.doctype = table
			self.validate_doctype()
			self.table = qb.DocType(table)

		if self.apply_permissions:
			self.check_read_permission()

		if update:
			self.query = qb.update(self.table, immutable=False)
		elif into:
			self.query = qb.into(self.table, immutable=False)
		elif delete:
			self.query = qb.from_(self.table, immutable=False).delete()
		else:
			self.query = qb.from_(self.table, immutable=False)
			self.apply_fields(fields)

		self.apply_filters(filters)
		self.apply_order_by(order_by)

		if limit:
			if not isinstance(limit, int) or limit < 0:
				frappe.throw(_("Limit must be a non-negative integer"), TypeError)
			self.query = self.query.limit(limit)

		if offset:
			if not isinstance(offset, int) or offset < 0:
				frappe.throw(_("Offset must be a non-negative integer"), TypeError)
			self.query = self.query.offset(offset)

		if distinct:
			self.query = self.query.distinct()

		if for_update:
			self.query = self.query.for_update(skip_locked=skip_locked, nowait=not wait)

		if group_by:
			self._validate_group_by(group_by)
			self.query = self.query.groupby(group_by)

		if self.apply_permissions:
			self.add_permission_conditions()

		self.query.immutable = True
		return self.query

	def validate_doctype(self):
		if not TABLE_NAME_PATTERN.match(self.doctype):
			frappe.throw(_("Invalid DocType: {0}").format(self.doctype))

	def apply_fields(self, fields):
		self.fields = self.parse_fields(fields)
		if self.apply_permissions:
			self.fields = self.apply_field_permissions()

		if not self.fields:
			self.fields = [self.table.name]

		self.query._child_queries = []
		for field in self.fields:
			if isinstance(field, DynamicTableField):
				self.query = field.apply_select(self.query)
			elif isinstance(field, ChildQuery):
				self.query._child_queries.append(field)
			else:
				self.query = self.query.select(field)

	def apply_filters(
		self,
		filters: dict[str, FilterValue] | FilterValue | list[list | FilterValue] | None = None,
	):
		if filters is None:
			return

		if isinstance(filters, FilterValue):
			filters = {"name": convert_to_value(filters)}

		if isinstance(filters, Criterion):
			self.query = self.query.where(filters)

		elif isinstance(filters, dict):
			self.apply_dict_filters(filters)

		elif isinstance(filters, list | tuple):
			if all(isinstance(d, FilterValue) for d in filters) and len(filters) > 0:
				self.apply_dict_filters({"name": ("in", tuple(convert_to_value(f) for f in filters))})
			else:
				for filter in filters:
					if isinstance(filter, FilterValue | Criterion | dict):
						self.apply_filters(filter)
					elif isinstance(filter, list | tuple):
						self.apply_list_filters(filter)
					else:
						raise ValueError(f"Unknown filter type: {type(filters)}")
		else:
			raise ValueError(f"Unknown filter type: {type(filters)}")

	def apply_list_filters(self, filter: list):
		if len(filter) == 2:
			field, value = filter
			self._apply_filter(field, value)
		elif len(filter) == 3:
			field, operator, value = filter
			self._apply_filter(field, value, operator)
		elif len(filter) == 4:
			doctype, field, operator, value = filter
			self._apply_filter(field, value, operator, doctype)
		else:
			raise ValueError(f"Unknown filter format: {filter}")

	def apply_dict_filters(self, filters: dict[str, FilterValue | list]):
		for field, value in filters.items():
			operator = "="
			if isinstance(value, list | tuple):
				operator, value = value

			self._apply_filter(field, value, operator)

	def _validate_and_prepare_filter_field(self, field: str | Field, doctype: str | None = None) -> Field:
		"""Validate field name for filters and return a pypika Field object. Handles dynamic fields."""
		_field = field
		is_fieldname_safe = False

		if not isinstance(_field, str):
			# Assume it's a pypika Field or similar, return as is.
			return _field

		# Always validate field name if it contains special characters to prevent injection
		if SPECIAL_CHAR_PATTERN.search(_field):
			# First, try to parse as a dynamic field (contains '.')
			dynamic_field = DynamicTableField.parse(_field, self.doctype)
			if dynamic_field:
				# Legitimate dynamic field (e.g., table.field), apply join
				self.query = dynamic_field.apply_join(self.query)
				_field = dynamic_field.field  # _field is now a pypika Field object
			# If not a dynamic field and doesn't match the allowed pattern, reject it
			elif not ALLOWED_SQL_FIELD_PATTERN.match(_field):
				frappe.throw(
					_(
						"Invalid filter field format: {0}. Field names cannot contain special characters or disallowed patterns."
					).format(_field),
					frappe.PermissionError,
				)
			# If it matched the pattern (e.g., `fieldname` with backticks), mark as safe
			else:
				is_fieldname_safe = True
		# No special characters, treat as a standard field name, mark as safe
		else:
			is_fieldname_safe = True

		# Convert string field name to pypika Field object if needed
		if is_fieldname_safe:
			# Note: We are converting the original `field` string here,
			# not the potentially modified `_field`
			# if it became a dynamic field object earlier.
			_field = frappe.qb.DocType(doctype or self.doctype)[field]

		return _field

	def _apply_filter(
		self,
		field: str | Field,
		value: FilterValue | list | set | None,
		operator: str = "=",
		doctype: str | None = None,
	):
		_field = self._validate_and_prepare_filter_field(field, doctype)
		_value = value
		_operator = operator

		# Apply implicit join if child table is referenced
		if doctype and doctype != self.doctype:
			meta = frappe.get_meta(doctype)
			table = frappe.qb.DocType(doctype)
			if meta.istable and not self.query.is_joined(table):
				self.query = self.query.left_join(table).on(
					(table.parent == self.table.name) & (table.parenttype == self.doctype)
				)

		_value = convert_to_value(_value)

		if not _value and isinstance(_value, list | tuple | set):
			_value = ("",)

			# Handle nested set operators
		if _operator in NESTED_SET_OPERATORS:
			hierarchy = _operator
			docname = _value

			# Use the original field name string for get_field if _field was converted
			original_field_name = field if isinstance(field, str) else _field.name
			_df = frappe.get_meta(self.doctype).get_field(original_field_name)
			ref_doctype = _df.options if _df else self.doctype

			nodes = get_nested_set_hierarchy_result(ref_doctype, docname, hierarchy)
			operator_fn = (
				OPERATOR_MAP["not in"]
				if hierarchy in ("not ancestors of", "not descendants of")
				else OPERATOR_MAP["in"]
			)
			self.query = self.query.where(operator_fn(_field, nodes or ("",)))
			return

		operator_fn = OPERATOR_MAP[_operator.casefold()]
		if _value is None and isinstance(_field, Field):
			self.query = self.query.where(_field.isnull())
		else:
			self.query = self.query.where(operator_fn(_field, _value))

	def get_function_object(self, field: str) -> "Function":
		"""Return PyPika Function object. Expect field to look like 'SUM(*)' or 'name' or something similar."""
		func = field.split("(", maxsplit=1)[0].capitalize()
		args_start, args_end = len(func) + 1, field.index(")")
		args = field[args_start:args_end].split(",")

		_, alias = field.split(" as ") if " as " in field else (None, None)

		to_cast = "*" not in args
		_args = []

		for arg in args:
			initial_fields = literal_eval_(arg.strip())
			if to_cast:
				has_primitive_operator = False
				for _operator in OPERATOR_MAP.keys():
					if _operator in initial_fields:
						operator_mapping = OPERATOR_MAP[_operator]
						# Only perform this if operator is of primitive type.
						if isinstance(operator_mapping, BuiltinFunctionType):
							has_primitive_operator = True
							field = operator_mapping(
								*map(
									lambda field: Field(field.strip())
									if "`" not in field
									else PseudoColumnMapper(field.strip()),
									arg.split(_operator),
								),
							)

				field = (
					(
						Field(initial_fields)
						if "`" not in initial_fields
						else PseudoColumnMapper(initial_fields)
					)
					if not has_primitive_operator
					else field
				)
			else:
				field = initial_fields

			_args.append(field)

		if alias and "`" in alias:
			alias = alias.replace("`", "")
		try:
			if func.casefold() == "now":
				return getattr(functions, func)()
			return getattr(functions, func)(*_args, alias=alias or None)
		except AttributeError:
			# Fall back for functions not present in `SqlFunctions``
			return Function(func, *_args, alias=alias or None)

	def parse_string_field(self, field: str):
		"""
		Parses a field string into a pypika Field object.

		Handles:
		- *
		- simple_field
		- `quoted_field`
		- tabDocType.simple_field
		- `tabDocType`.`quoted_field`
		- Aliases for all above formats (e.g., field as alias)
		"""
		if field == "*":
			return self.table.star

		alias = None
		field_part = field
		if " as " in field.lower():  # Case-insensitive check for ' as '
			# Find the last occurrence of ' as ' to handle potential aliases named 'as'
			parts = re.split(r"\s+as\s+", field, flags=re.IGNORECASE)
			if len(parts) > 1:
				field_part = parts[0].strip()
				alias = parts[1].strip().strip('`"')  # Remove potential quotes from alias

		match = FIELD_PARSE_REGEX.match(field_part)

		if not match:
			frappe.throw(_("Could not parse field: {0}").format(field))

		table_name, field_name = match.groups()

		if table_name:
			# Table name specified (e.g., `tabX`.`y` or tabX.y)
			table_obj = frappe.qb.DocType(table_name)
			pypika_field = table_obj[field_name]
		else:
			# Simple field name (e.g., `y` or y) - use the main table
			pypika_field = self.table[field_name]

		if alias:
			return pypika_field.as_(alias)
		else:
			return pypika_field

	def _parse_single_field_item(
		self, field: str | Criterion | dict
	) -> list | Criterion | Field | "DynamicTableField" | "ChildQuery" | None:
		"""Parses a single item from the fields list/tuple. Assumes comma-separated strings have already been split."""
		if isinstance(field, Criterion):
			return field
		elif isinstance(field, dict):
			# Handle child queries defined as dicts {fieldname: [child_fields]}
			_parsed_fields = []
			for child_field, child_fields_list in field.items():
				# Ensure child_fields_list is a list or tuple
				if not isinstance(child_fields_list, list | tuple):
					frappe.throw(
						_("Child query fields for '{0}' must be a list or tuple.").format(child_field)
					)
				_parsed_fields.append(ChildQuery(child_field, list(child_fields_list), self.doctype))
			# Return list as a dict entry might represent multiple child queries (though unlikely)
			return _parsed_fields

		# At this point, field must be a string (already validated and sanitized)
		if not isinstance(field, str):
			frappe.throw(_("Invalid field type: {0}").format(type(field)))

		# Check for functions or dynamic fields first
		if has_function(field):
			return self.get_function_object(field)
		elif parsed := DynamicTableField.parse(field, self.doctype):
			return parsed
		# Otherwise, parse as a standard field (simple, quoted, table-qualified, with/without alias)
		else:
			# Note: Comma handling is done in parse_fields before this method is called
			return self.parse_string_field(field)

	def parse_fields(
		self, fields: str | list | tuple | None
	) -> list[Field | Criterion | "DynamicTableField" | "ChildQuery"]:
		if not fields:
			return []

		sanitized_field_list = []
		if isinstance(fields, str):
			# Split comma-separated fields passed as a single string *before* sanitizing
			sanitized_field_list.extend(
				_sanitize_field(f.strip(), self.is_mariadb) for f in COMMA_PATTERN.split(fields) if f.strip()
			)
		elif isinstance(fields, list | tuple):
			# Sanitize fields if input is already a list/tuple
			sanitized_field_list.extend(
				_sanitize_field(field, self.is_mariadb) if isinstance(field, str) else field
				for field in fields
			)
		else:
			frappe.throw(_("Fields must be a string, list, or tuple"))

		_fields = []
		# Iterate through the list where each item is a single field definition or criterion
		for field_item in sanitized_field_list:
			parsed = self._parse_single_field_item(field_item)
			if isinstance(parsed, list):  # Result from parsing a child query dict
				_fields.extend(parsed)
			elif parsed:
				_fields.append(parsed)

		return _fields

	def _validate_group_by(self, group_by: str):
		"""Validate the group_by string argument."""
		if not isinstance(group_by, str):
			frappe.throw(_("Group By must be a string"), TypeError)
		parts = COMMA_PATTERN.split(group_by)
		for part in parts:
			field_name = part.strip()
			if not field_name:
				continue
			if field_name.isdigit():
				continue
			if not ALLOWED_SQL_FIELD_PATTERN.match(field_name):
				frappe.throw(
					_("Invalid field format in Group By: {0}").format(field_name),
					frappe.PermissionError,
				)

	def apply_order_by(self, order_by: str | None):
		if not order_by or order_by == DefaultOrderBy:
			return

		self._validate_order_by(order_by)

		for declaration in order_by.split(","):
			if _order_by := declaration.strip():
				parts = _order_by.split(" ")
				order_field = parts[0]
				order_direction = Order.asc if (len(parts) > 1 and parts[1].lower() == "asc") else Order.desc
				self.query = self.query.orderby(order_field, order=order_direction)

	def _validate_order_by(self, order_by: str):
		"""Validate the order_by string argument."""
		if not isinstance(order_by, str):
			frappe.throw(_("Order By must be a string"), TypeError)

		valid_directions = {"asc", "desc"}

		for declaration in order_by.split(","):
			if _order_by := declaration.strip():
				parts = _order_by.split()
				field_name = parts[0]
				direction = None
				if len(parts) > 1:
					direction = parts[1].lower()

				if field_name.isdigit():
					pass
				elif not ALLOWED_SQL_FIELD_PATTERN.match(field_name):
					frappe.throw(
						_("Invalid field format in Order By: {0}").format(field_name),
						frappe.PermissionError,
					)

				if direction and direction not in valid_directions:
					frappe.throw(
						_("Invalid direction in Order By: {0}. Must be 'ASC' or 'DESC'.").format(parts[1]),
						ValueError,
					)

	def check_read_permission(self):
		"""Check if user has read permission on the doctype"""

		def has_permission(ptype):
			return frappe.has_permission(
				self.doctype,
				ptype,
				user=self.user,
				parent_doctype=self.parent_doctype,
			)

		if not has_permission("select") and not has_permission("read"):
			frappe.throw(
				_("Insufficient Permission for {0}").format(frappe.bold(self.doctype)), frappe.PermissionError
			)

	def apply_field_permissions(self):
		"""Filter the list of fields based on permlevel."""
		allowed_fields = []
		permitted_fields_set = set(
			get_permitted_fields(
				doctype=self.doctype,
				parenttype=self.parent_doctype,
				permission_type=self.get_permission_type(self.doctype),
				ignore_virtual=True,
			)
		)

		for field in self.fields:
			if isinstance(field, ChildTableField):
				# Cache permitted fields for child doctypes if accessed multiple times
				permitted_child_fields_set = set(
					get_permitted_fields(
						doctype=field.doctype,
						parenttype=field.parent_doctype,
						permission_type=self.get_permission_type(field.doctype),
						ignore_virtual=True,
					)
				)
				# Check permission for the specific field in the child table
				if field.fieldname in permitted_child_fields_set:
					allowed_fields.append(field)
			elif isinstance(field, LinkTableField):
				# Check permission for the link field *in the parent doctype*
				if field.link_fieldname in permitted_fields_set:
					allowed_fields.append(field)
			elif isinstance(field, ChildQuery):
				# Cache permitted fields for the child doctype of the query
				permitted_child_fields_set = set(
					get_permitted_fields(
						doctype=field.doctype,
						parenttype=field.parent_doctype,
						permission_type=self.get_permission_type(field.doctype),
						ignore_virtual=True,
					)
				)
				# Filter the fields *within* the ChildQuery object based on permissions
				field.fields = [f for f in field.fields if f in permitted_child_fields_set]
				# Only add the child query if it still has fields after filtering
				if field.fields:
					allowed_fields.append(field)
			elif isinstance(field, Field):
				if field.name == "*":
					# Expand '*' to include all permitted fields
					# Avoid reparsing '*' recursively by passing the actual list
					allowed_fields.extend(self.parse_fields(list(permitted_fields_set)))
				# Check if the field name (without alias) is permitted
				elif field.name in permitted_fields_set:
					allowed_fields.append(field)
				# Handle cases where the field might be aliased but the base name is permitted
				elif hasattr(field, "alias") and field.alias and field.name in permitted_fields_set:
					allowed_fields.append(field)

			elif isinstance(field, PseudoColumnMapper):
				# Typically functions or complex terms
				allowed_fields.append(field)

		return allowed_fields

	def get_user_permission_conditions(self, role_permissions):
		"""Build conditions for user permissions and return tuple of (conditions, fetch_shared_docs)"""
		conditions = []
		fetch_shared_docs = False

		# add user permission only if role has read perm
		if not (role_permissions.get("read") or role_permissions.get("select")):
			return conditions, fetch_shared_docs

		user_permissions = frappe.permissions.get_user_permissions(self.user)

		if not user_permissions:
			return conditions, fetch_shared_docs

		fetch_shared_docs = True

		doctype_link_fields = self.get_doctype_link_fields()
		for df in doctype_link_fields:
			if df.get("ignore_user_permissions"):
				continue

			user_permission_values = user_permissions.get(df.get("options"), {})
			if user_permission_values:
				docs = []
				for permission in user_permission_values:
					if not permission.get("applicable_for"):
						docs.append(permission.get("doc"))
					# append docs based on user permission applicable on reference doctype
					# this is useful when getting list of docs from a link field
					# in this case parent doctype of the link
					# will be the reference doctype
					elif df.get("fieldname") == "name" and self.reference_doctype:
						if permission.get("applicable_for") == self.reference_doctype:
							docs.append(permission.get("doc"))
					elif permission.get("applicable_for") == self.doctype:
						docs.append(permission.get("doc"))

				if docs:
					field_name = df.get("fieldname")
					strict_user_permissions = frappe.get_system_settings("apply_strict_user_permissions")
					if strict_user_permissions:
						conditions.append(self.table[field_name].isin(docs))
					else:
						empty_value_condition = self.table[field_name].isnull()
						value_condition = self.table[field_name].isin(docs)
						conditions.append(empty_value_condition | value_condition)

		return conditions, fetch_shared_docs

	def get_doctype_link_fields(self):
		meta = frappe.get_meta(self.doctype)
		# append current doctype with fieldname as 'name' as first link field
		doctype_link_fields = [{"options": self.doctype, "fieldname": "name"}]
		# append other link fields
		doctype_link_fields.extend(meta.get_link_fields())
		return doctype_link_fields

	def add_permission_conditions(self):
		conditions = []
		role_permissions = frappe.permissions.get_role_permissions(self.doctype, user=self.user)
		fetch_shared_docs = False

		if self.requires_owner_constraint(role_permissions):
			fetch_shared_docs = True
			conditions.append(self.table.owner == self.user)
		# skip user perm check if owner constraint is required
		elif role_permissions.get("read") or role_permissions.get("select"):
			user_perm_conditions, fetch_shared = self.get_user_permission_conditions(role_permissions)
			conditions.extend(user_perm_conditions)
			fetch_shared_docs = fetch_shared_docs or fetch_shared

		permission_query_conditions = self.get_permission_query_conditions()
		if permission_query_conditions:
			conditions.extend(permission_query_conditions)

		shared_docs = []
		if fetch_shared_docs:
			shared_docs = frappe.share.get_shared(self.doctype, self.user)

		if shared_docs:
			shared_condition = self.table.name.isin(shared_docs)
			if conditions:
				# (permission conditions) OR (shared condition)
				self.query = self.query.where(Criterion.all(conditions) | shared_condition)
			else:
				self.query = self.query.where(shared_condition)
		elif conditions:
			# AND all permission conditions
			self.query = self.query.where(Criterion.all(conditions))

	def get_permission_query_conditions(self):
		"""Add permission query conditions from hooks and server scripts"""
		from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

		conditions = []
		hooks = frappe.get_hooks("permission_query_conditions", {})
		condition_methods = hooks.get(self.doctype, []) + hooks.get("*", [])

		for method in condition_methods:
			if c := frappe.call(frappe.get_attr(method), self.user, doctype=self.doctype):
				conditions.append(RawCriterion(c))

		# Get conditions from server scripts
		if permission_script_name := get_server_script_map().get("permission_query", {}).get(self.doctype):
			script = frappe.get_doc("Server Script", permission_script_name)
			if condition := script.get_permission_query_conditions(self.user):
				conditions.append(RawCriterion(condition))

		return conditions

	def get_permission_type(self, doctype) -> str:
		"""Get permission type (select/read) based on user permissions"""
		if frappe.only_has_select_perm(doctype, user=self.user):
			return "select"
		return "read"

	def requires_owner_constraint(self, role_permissions):
		"""Return True if "select" or "read" isn't available without being creator."""
		if not role_permissions.get("has_if_owner_enabled"):
			return

		if_owner_perms = role_permissions.get("if_owner")
		if not if_owner_perms:
			return

		# has select or read without if owner, no need for constraint
		for perm_type in ("select", "read"):
			if role_permissions.get(perm_type) and perm_type not in if_owner_perms:
				return

		# not checking if either select or read if present in if_owner_perms
		# because either of those is required to perform a query
		return True


class Permission:
	@classmethod
	def check_permissions(cls, query, **kwargs):
		if not isinstance(query, str):
			query = query.get_sql()

		doctype = cls.get_tables_from_query(query)
		if isinstance(doctype, str):
			doctype = [doctype]

		for dt in doctype:
			dt = TAB_PATTERN.sub("", dt)
			if not frappe.has_permission(
				dt,
				"select",
				user=kwargs.get("user"),
				parent_doctype=kwargs.get("parent_doctype"),
			) and not frappe.has_permission(
				dt,
				"read",
				user=kwargs.get("user"),
				parent_doctype=kwargs.get("parent_doctype"),
			):
				frappe.throw(
					_("Insufficient Permission for {0}").format(frappe.bold(dt)), frappe.PermissionError
				)

	@staticmethod
	def get_tables_from_query(query: str):
		return [table for table in WORDS_PATTERN.findall(query) if table.startswith("tab")]


class DynamicTableField:
	def __init__(
		self,
		doctype: str,
		fieldname: str,
		parent_doctype: str,
		alias: str | None = None,
	) -> None:
		self.doctype = doctype
		self.fieldname = fieldname
		self.alias = alias
		self.parent_doctype = parent_doctype

	def __str__(self) -> str:
		table_name = f"`tab{self.doctype}`"
		fieldname = f"`{self.fieldname}`"
		if frappe.db.db_type == "postgres":
			table_name = table_name.replace("`", '"')
			fieldname = fieldname.replace("`", '"')
		alias = f"AS {self.alias}" if self.alias else ""
		return f"{table_name}.{fieldname} {alias}".strip()

	@staticmethod
	def parse(field: str, doctype: str):
		if "." in field:
			alias = None
			if " as " in field:
				field, alias = field.split(" as ")
			if field.startswith("`tab") or field.startswith('"tab'):
				_, child_doctype, child_field = re.search(r'([`"])tab(.+?)\1.\1(.+)\1', field).groups()
				if child_doctype == doctype:
					return
				return ChildTableField(child_doctype, child_field, doctype, alias=alias)
			else:
				linked_fieldname, fieldname = field.split(".")
				linked_field = frappe.get_meta(doctype).get_field(linked_fieldname)
				linked_doctype = linked_field.options
				if linked_field.fieldtype == "Link":
					return LinkTableField(linked_doctype, fieldname, doctype, linked_fieldname, alias=alias)
				elif linked_field.fieldtype in frappe.model.table_fields:
					return ChildTableField(linked_doctype, fieldname, doctype, linked_fieldname, alias=alias)

	def apply_select(self, query: QueryBuilder) -> QueryBuilder:
		raise NotImplementedError


class ChildTableField(DynamicTableField):
	def __init__(
		self,
		doctype: str,
		fieldname: str,
		parent_doctype: str,
		parent_fieldname: str | None = None,
		alias: str | None = None,
	) -> None:
		self.doctype = doctype
		self.fieldname = fieldname
		self.alias = alias
		self.parent_doctype = parent_doctype
		self.parent_fieldname = parent_fieldname
		self.table = frappe.qb.DocType(self.doctype)
		self.field = self.table[self.fieldname]

	def apply_select(self, query: QueryBuilder) -> QueryBuilder:
		table = frappe.qb.DocType(self.doctype)
		query = self.apply_join(query)
		return query.select(getattr(table, self.fieldname).as_(self.alias or None))

	def apply_join(self, query: QueryBuilder) -> QueryBuilder:
		table = frappe.qb.DocType(self.doctype)
		main_table = frappe.qb.DocType(self.parent_doctype)
		if not query.is_joined(table):
			query = query.left_join(table).on(
				(table.parent == main_table.name) & (table.parenttype == self.parent_doctype)
			)
		return query


class LinkTableField(DynamicTableField):
	def __init__(
		self,
		doctype: str,
		fieldname: str,
		parent_doctype: str,
		link_fieldname: str,
		alias: str | None = None,
	) -> None:
		super().__init__(doctype, fieldname, parent_doctype, alias=alias)
		self.link_fieldname = link_fieldname
		self.table = frappe.qb.DocType(self.doctype)
		self.field = self.table[self.fieldname]

	def apply_select(self, query: QueryBuilder) -> QueryBuilder:
		table = frappe.qb.DocType(self.doctype)
		query = self.apply_join(query)
		return query.select(getattr(table, self.fieldname).as_(self.alias or None))

	def apply_join(self, query: QueryBuilder) -> QueryBuilder:
		table = frappe.qb.DocType(self.doctype)
		main_table = frappe.qb.DocType(self.parent_doctype)
		if not query.is_joined(table):
			query = query.left_join(table).on(table.name == getattr(main_table, self.link_fieldname))
		return query


class ChildQuery:
	def __init__(
		self,
		fieldname: str,
		fields: list,
		parent_doctype: str,
	) -> None:
		field = frappe.get_meta(parent_doctype).get_field(fieldname)
		if field.fieldtype not in frappe.model.table_fields:
			return
		self.fieldname = fieldname
		self.fields = fields
		self.parent_doctype = parent_doctype
		self.doctype = field.options

	def get_query(self, parent_names=None) -> QueryBuilder:
		filters = {
			"parenttype": self.parent_doctype,
			"parentfield": self.fieldname,
			"parent": ["in", parent_names],
		}
		return frappe.qb.get_query(
			self.doctype,
			fields=[*self.fields, "parent", "parentfield"],
			filters=filters,
			order_by="idx asc",
		)


def literal_eval_(literal):
	try:
		return literal_eval(literal)
	except (ValueError, SyntaxError):
		return literal


def has_function(field: str):
	if "`" not in field:
		field = field.casefold()

	return any(func in field for func in SQL_FUNCTIONS)


def get_nested_set_hierarchy_result(doctype: str, name: str, hierarchy: str) -> list[str]:
	"""Get matching nodes based on operator."""
	table = frappe.qb.DocType(doctype)
	try:
		lft, rgt = frappe.qb.from_(table).select("lft", "rgt").where(table.name == name).run()[0]
	except IndexError:
		lft, rgt = None, None

	if hierarchy in ("descendants of", "not descendants of", "descendants of (inclusive)"):
		result = (
			frappe.qb.from_(table)
			.select(table.name)
			.where(table.lft > lft)
			.where(table.rgt < rgt)
			.orderby(table.lft, order=Order.asc)
			.run(pluck=True)
		)
		if hierarchy == "descendants of (inclusive)":
			result += [name]
	else:
		# Get ancestor elements of a DocType with a tree structure
		result = (
			frappe.qb.from_(table)
			.select(table.name)
			.where(table.lft < lft)
			.where(table.rgt > rgt)
			.orderby(table.lft, order=Order.desc)
			.run(pluck=True)
		)
	return result


@lru_cache(maxsize=1024)
def _validate_select_field(field: str):
	"""Validate a field string intended for use in a SELECT clause."""
	if field == "*":
		return

	if field.isdigit():
		return

	if ALLOWED_FIELD_PATTERN.match(field) or FUNCTION_CALL_PATTERN.match(field):
		return

	frappe.throw(
		_(
			"Invalid field format for SELECT: {0}. Field names must be simple, backticked, table-qualified, aliased, a valid function call, or '*'."
		).format(field),
		frappe.PermissionError,
	)


@lru_cache(maxsize=1024)
def _sanitize_field(field: str, is_mariadb):
	"""Validate and sanitize a field string for SELECT clause by stripping comments."""
	_validate_select_field(field)

	stripped_field = sqlparse.format(field, strip_comments=True, keyword_case="lower")

	if is_mariadb:
		stripped_field = MARIADB_SPECIFIC_COMMENT.sub("", stripped_field)

	return stripped_field.strip()


class RawCriterion(Term):
	"""A class to represent raw SQL string as a criterion.

	Allows using raw SQL strings in pypika queries:
		frappe.qb.from_("DocType").where(RawCriterion("name like 'a%'"))
	"""

	def __init__(self, sql_string: str):
		self.sql_string = sql_string
		super().__init__()

	def get_sql(self, **kwargs: Any) -> str:
		return self.sql_string

	def __and__(self, other):
		return CombinedRawCriterion(self, other, "AND")

	def __or__(self, other):
		return CombinedRawCriterion(self, other, "OR")

	def __invert__(self):
		return RawCriterion(f"NOT ({self.sql_string})")


class CombinedRawCriterion(RawCriterion):
	def __init__(self, left, right, operator):
		self.left = left
		self.right = right
		self.operator = operator
		super(RawCriterion, self).__init__()

	def get_sql(self, **kwargs: Any) -> str:
		left_sql = self.left.get_sql(**kwargs) if hasattr(self.left, "get_sql") else str(self.left)
		right_sql = self.right.get_sql(**kwargs) if hasattr(self.right, "get_sql") else str(self.right)
		return f"({left_sql}) {self.operator} ({right_sql})"

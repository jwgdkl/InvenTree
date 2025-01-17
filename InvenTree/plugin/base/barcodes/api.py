"""API endpoints for barcode plugins."""

import logging

from django.urls import path, re_path
from django.utils.translation import gettext_lazy as _

from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from InvenTree.helpers import hash_barcode
from order.models import PurchaseOrder
from plugin import registry
from plugin.builtin.barcodes.inventree_barcode import \
    InvenTreeInternalBarcodePlugin
from stock.models import StockLocation
from users.models import RuleSet

logger = logging.getLogger('inventree')


class BarcodeScan(APIView):
    """Endpoint for handling generic barcode scan requests.

    Barcode data are decoded by the client application,
    and sent to this endpoint (as a JSON object) for validation.

    A barcode could follow the internal InvenTree barcode format,
    or it could match to a third-party barcode format (e.g. Digikey).

    When a barcode is sent to the server, the following parameters must be provided:

    - barcode: The raw barcode data

    plugins:
    Third-party barcode formats may be supported using 'plugins'
    (more information to follow)

    hashing:
    Barcode hashes are calculated using MD5
    """

    permission_classes = [
        permissions.IsAuthenticated,
    ]

    def post(self, request, *args, **kwargs):
        """Respond to a barcode POST request.

        Check if required info was provided and then run though the plugin steps or try to match up-
        """
        data = request.data

        barcode_data = data.get('barcode', None)

        if not barcode_data:
            raise ValidationError({'barcode': _('Missing barcode data')})

        # Note: the default barcode handlers are loaded (and thus run) first
        plugins = registry.with_mixin('barcode')

        barcode_hash = hash_barcode(barcode_data)

        # Look for a barcode plugin which knows how to deal with this barcode
        plugin = None
        response = {}

        for current_plugin in plugins:

            result = current_plugin.scan(barcode_data)

            if result is None:
                continue

            if "error" in result:
                logger.info("%s.scan(...) returned an error: %s",
                            current_plugin.__class__.__name__, result["error"])
                if not response:
                    plugin = current_plugin
                    response = result
            else:
                plugin = current_plugin
                response = result
                break

        response['plugin'] = plugin.name if plugin else None
        response['barcode_data'] = barcode_data
        response['barcode_hash'] = barcode_hash

        # A plugin has not been found!
        if plugin is None:
            response['error'] = _('No match found for barcode data')

            raise ValidationError(response)
        else:
            response['success'] = _('Match found for barcode data')
            return Response(response)


class BarcodeAssign(APIView):
    """Endpoint for assigning a barcode to a stock item.

    - This only works if the barcode is not already associated with an object in the database
    - If the barcode does not match an object, then the barcode hash is assigned to the StockItem
    """

    permission_classes = [
        permissions.IsAuthenticated
    ]

    def post(self, request, *args, **kwargs):
        """Respond to a barcode assign POST request.

        Checks inputs and assign barcode (hash) to StockItem.
        """
        data = request.data

        barcode_data = data.get('barcode', None)

        if not barcode_data:
            raise ValidationError({'barcode': _('Missing barcode data')})

        # Here we only check against 'InvenTree' plugins
        plugins = registry.with_mixin('barcode', builtin=True)

        # First check if the provided barcode matches an existing database entry
        for plugin in plugins:
            result = plugin.scan(barcode_data)

            if result is not None:
                result["error"] = _("Barcode matches existing item")
                result["plugin"] = plugin.name
                result["barcode_data"] = barcode_data

                raise ValidationError(result)

        barcode_hash = hash_barcode(barcode_data)

        valid_labels = []

        for model in InvenTreeInternalBarcodePlugin.get_supported_barcode_models():
            label = model.barcode_model_type()
            valid_labels.append(label)

            if label in data:
                try:
                    instance = model.objects.get(pk=data[label])

                    # Check that the user has the required permission
                    app_label = model._meta.app_label
                    model_name = model._meta.model_name

                    table = f"{app_label}_{model_name}"

                    if not RuleSet.check_table_permission(request.user, table, "change"):
                        raise PermissionDenied({
                            "error": f"You do not have the required permissions for {table}"
                        })

                    instance.assign_barcode(
                        barcode_data=barcode_data,
                        barcode_hash=barcode_hash,
                    )

                    return Response({
                        'success': f"Assigned barcode to {label} instance",
                        label: {
                            'pk': instance.pk,
                        },
                        "barcode_data": barcode_data,
                        "barcode_hash": barcode_hash,
                    })

                except (ValueError, model.DoesNotExist):
                    raise ValidationError({
                        'error': f"No matching {label} instance found in database",
                    })

        # If we got here, it means that no valid model types were provided
        raise ValidationError({
            'error': f"Missing data: provide one of '{valid_labels}'",
        })


class BarcodeUnassign(APIView):
    """Endpoint for unlinking / unassigning a custom barcode from a database object"""

    permission_classes = [
        permissions.IsAuthenticated,
    ]

    def post(self, request, *args, **kwargs):
        """Respond to a barcode unassign POST request"""
        # The following database models support assignment of third-party barcodes
        supported_models = InvenTreeInternalBarcodePlugin.get_supported_barcode_models()

        supported_labels = [model.barcode_model_type() for model in supported_models]
        model_names = ', '.join(supported_labels)

        data = request.data

        matched_labels = []

        for label in supported_labels:
            if label in data:
                matched_labels.append(label)

        if len(matched_labels) == 0:
            raise ValidationError({
                'error': f"Missing data: Provide one of '{model_names}'"
            })

        if len(matched_labels) > 1:
            raise ValidationError({
                'error': f"Multiple conflicting fields: '{model_names}'",
            })

        # At this stage, we know that we have received a single valid field
        for model in supported_models:
            label = model.barcode_model_type()

            if label in data:
                try:
                    instance = model.objects.get(pk=data[label])
                except (ValueError, model.DoesNotExist):
                    raise ValidationError({
                        label: _('No match found for provided value')
                    })

                # Check that the user has the required permission
                app_label = model._meta.app_label
                model_name = model._meta.model_name

                table = f"{app_label}_{model_name}"

                if not RuleSet.check_table_permission(request.user, table, "change"):
                    raise PermissionDenied({
                        "error": f"You do not have the required permissions for {table}"
                    })

                # Unassign the barcode data from the model instance
                instance.unassign_barcode()

                return Response({
                    'success': f'Barcode unassigned from {label} instance',
                })

        # If we get to this point, something has gone wrong!
        raise ValidationError({
            'error': 'Could not unassign barcode',
        })


class BarcodePOReceive(APIView):
    """Endpoint for handling receiving parts by scanning their barcode.

    Barcode data are decoded by the client application,
    and sent to this endpoint (as a JSON object) for validation.

    The barcode should follow a third-party barcode format (e.g. Digikey)
    and ideally contain order_number and quantity information.

    The following parameters are available:

    - barcode: The raw barcode data (required)
    - purchase_order: The purchase order containing the item to receive (optional)
    - location: The destination location for the received item (optional)
    """

    permission_classes = [
        permissions.IsAuthenticated,
    ]

    def post(self, request, *args, **kwargs):
        """Respond to a barcode POST request."""

        data = request.data

        if not (barcode_data := data.get("barcode")):
            raise ValidationError({"barcode": _("Missing barcode data")})

        logger.debug("BarcodePOReceive: scanned barcode - '%s'", barcode_data)

        purchase_order = None

        if purchase_order_pk := data.get("purchase_order"):
            purchase_order = PurchaseOrder.objects.filter(pk=purchase_order_pk).first()
            if not purchase_order:
                raise ValidationError({"purchase_order": _("Invalid purchase order")})

        location = None
        if (location_pk := data.get("location")):
            location = StockLocation.objects.get(pk=location_pk)
            if not location:
                raise ValidationError({"location": _("Invalid stock location")})

        plugins = registry.with_mixin("barcode")

        # Look for a barcode plugin which knows how to deal with this barcode
        plugin = None
        response = {}

        internal_barcode_plugin = next(filter(
            lambda plugin: plugin.name == "InvenTreeBarcode", plugins))
        if internal_barcode_plugin.scan(barcode_data):
            response["error"] = _("Item has already been received")
            raise ValidationError(response)

        # Now, look just for "supplier-barcode" plugins
        plugins = registry.with_mixin("supplier-barcode")

        for current_plugin in plugins:

            result = current_plugin.scan_receive_item(
                barcode_data,
                request.user,
                purchase_order=purchase_order,
                location=location,
            )

            if result is None:
                continue

            if "error" in result:
                logger.info("%s.scan_receive_item(...) returned an error: %s",
                            current_plugin.__class__.__name__, result["error"])
                if not response:
                    plugin = current_plugin
                    response = result
            else:
                plugin = current_plugin
                response = result
                break

        response["plugin"] = plugin.name if plugin else None
        response["barcode_data"] = barcode_data
        response["barcode_hash"] = hash_barcode(barcode_data)

        # A plugin has not been found!
        if plugin is None:
            response["error"] = _("No match for supplier barcode")
            raise ValidationError(response)
        elif "error" in response:
            raise ValidationError(response)
        else:
            return Response(response)


barcode_api_urls = [
    # Link a third-party barcode to an item (e.g. Part / StockItem / etc)
    path('link/', BarcodeAssign.as_view(), name='api-barcode-link'),

    # Unlink a third-party barcode from an item
    path('unlink/', BarcodeUnassign.as_view(), name='api-barcode-unlink'),

    # Receive a purchase order item by scanning its barcode
    path("po-receive/", BarcodePOReceive.as_view(), name="api-barcode-po-receive"),

    # Catch-all performs barcode 'scan'
    re_path(r'^.*$', BarcodeScan.as_view(), name='api-barcode-scan'),
]

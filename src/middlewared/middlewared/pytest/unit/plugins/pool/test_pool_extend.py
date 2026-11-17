import errno
import unittest
from unittest.mock import Mock, call

from middlewared.plugins.pool_.info import PoolService as PoolInfoService
from middlewared.plugins.pool_.pool import PoolService
from middlewared.service import CallError, ValidationError


def pool_row(oid=1, name='tank'):
    return {'id': oid, 'name': name, 'guid': str(oid), 'all_sed': False}


def normalized_pool(status):
    # Include the public schema's boolean default as a merge-order guard;
    # the current internal normalize call can omit this field.
    return {
        'status': status,
        'is_upgraded': False,
        'healthy': status == 'ONLINE',
        'warning': status == 'DEGRADED',
        'status_code': 'DEVICE_FAULTED' if status == 'DEGRADED' else None,
        'status_detail': 'One device is faulted' if status == 'DEGRADED' else None,
        'topology': None if status == 'OFFLINE' else {'data': []},
        'size': None if status == 'OFFLINE' else 1024,
    }


class PoolExtendTests(unittest.TestCase):
    def test_mixed_imported_and_offline_pool_query_keeps_all_rows(self):
        middleware = Mock()
        service = PoolService(middleware)
        rows = [pool_row(), pool_row(2, 'exported')]
        context = service.pool_extend_context(rows, {'is_upgraded': True})

        def call_sync(method, *args):
            if method == 'pool.pool_normalize_info':
                return normalized_pool('ONLINE' if args[0] == 'tank' else 'OFFLINE')
            if method == 'pool.is_upgraded':
                if args[0] == 2:
                    raise ValidationError('pool.is_upgraded', 'pool is not imported', errno.ENOENT)
                return True
            raise AssertionError(method)

        middleware.call_sync.side_effect = call_sync
        result = [service.pool_extend(row, context) for row in rows]

        self.assertEqual([row['name'] for row in result], ['tank', 'exported'])
        self.assertEqual(result[0]['status'], 'ONLINE')
        self.assertIs(result[0]['is_upgraded'], True)
        self.assertEqual(result[1]['status'], 'OFFLINE')
        self.assertIs(result[1]['is_upgraded'], False)
        self.assertIsNone(result[1]['topology'])
        self.assertEqual(middleware.call_sync.call_args_list, [
            call('pool.pool_normalize_info', 'tank', context['sed_cache'], False),
            call('pool.is_upgraded', 1),
            call('pool.pool_normalize_info', 'exported', context['sed_cache'], False),
        ])

    def test_imported_pool_preserves_its_upgrade_result(self):
        for status in ['ONLINE', 'DEGRADED']:
            for is_upgraded in [True, False]:
                with self.subTest(status=status, is_upgraded=is_upgraded):
                    middleware = Mock()
                    service = PoolService(middleware)
                    context = service.pool_extend_context([], {'is_upgraded': True})
                    normalized = normalized_pool(status)
                    middleware.call_sync.side_effect = lambda method, *args: (
                        normalized.copy() if method == 'pool.pool_normalize_info' else is_upgraded
                    )

                    result = service.pool_extend(pool_row(), context)

                    self.assertEqual(result['status'], status)
                    self.assertIs(result['is_upgraded'], is_upgraded)
                    for field in ['healthy', 'warning', 'status_code', 'status_detail', 'size', 'topology']:
                        self.assertEqual(result[field], normalized[field])
                    self.assertEqual(middleware.call_sync.call_args_list, [
                        call('pool.pool_normalize_info', 'tank', context['sed_cache'], False),
                        call('pool.is_upgraded', 1),
                    ])

    def test_query_without_upgrade_extra_only_normalizes(self):
        for extra in [{}, {'is_upgraded': False}]:
            for status in ['ONLINE', 'OFFLINE']:
                with self.subTest(extra=extra, status=status):
                    middleware = Mock()
                    service = PoolService(middleware)
                    context = service.pool_extend_context([], extra)
                    middleware.call_sync.return_value = normalized_pool(status)

                    result = service.pool_extend(pool_row(), context)

                    self.assertEqual(result['status'], status)
                    self.assertIs(result['is_upgraded'], False)
                    middleware.call_sync.assert_called_once_with(
                        'pool.pool_normalize_info', 'tank', context['sed_cache'], False,
                    )

    def test_offline_pool_without_normalized_upgrade_field_still_returns_boolean(self):
        middleware = Mock()
        service = PoolService(middleware)
        context = service.pool_extend_context([], {'is_upgraded': True})
        normalized = normalized_pool('OFFLINE')
        normalized.pop('is_upgraded')
        middleware.call_sync.return_value = normalized

        result = service.pool_extend(pool_row(), context)

        self.assertIs(result['is_upgraded'], False)
        middleware.call_sync.assert_called_once_with(
            'pool.pool_normalize_info', 'tank', context['sed_cache'], False,
        )

    def test_pool_exported_during_upgrade_check_is_refreshed_as_offline(self):
        middleware = Mock()
        service = PoolService(middleware)
        context = service.pool_extend_context([], {'is_upgraded': True})
        middleware.call_sync.side_effect = [
            normalized_pool('ONLINE'),
            ValidationError('pool.is_upgraded', 'pool is not imported', errno.ENOENT),
            normalized_pool('OFFLINE'),
        ]

        result = service.pool_extend(pool_row(), context)

        self.assertEqual(result['status'], 'OFFLINE')
        self.assertIs(result['is_upgraded'], False)
        self.assertIs(result['healthy'], False)
        self.assertIsNone(result['topology'])
        self.assertIsNone(result['size'])
        self.assertEqual(middleware.call_sync.call_args_list, [
            call('pool.pool_normalize_info', 'tank', context['sed_cache'], False),
            call('pool.is_upgraded', 1),
            call('pool.pool_normalize_info', 'tank', context['sed_cache'], False),
        ])

    def test_missing_upgrade_state_is_not_hidden_when_pool_is_still_imported(self):
        for status in ['ONLINE', 'DEGRADED']:
            with self.subTest(status=status):
                middleware = Mock()
                service = PoolService(middleware)
                context = service.pool_extend_context([], {'is_upgraded': True})
                error = ValidationError('pool.is_upgraded', 'feature state missing', errno.ENOENT)
                middleware.call_sync.side_effect = [normalized_pool('ONLINE'), error, normalized_pool(status)]

                with self.assertRaises(ValidationError) as caught:
                    service.pool_extend(pool_row(), context)

                self.assertIs(caught.exception, error)
                self.assertEqual(middleware.call_sync.call_count, 3)

    def test_upgrade_errors_other_than_missing_pool_propagate_without_requery(self):
        errors = [
            ValidationError('pool.is_upgraded', 'permission denied', errno.EPERM),
            ValidationError('pool.is_upgraded', 'invalid feature state', errno.EINVAL),
            CallError('pool query failed', errno.EIO),
            RuntimeError('unexpected upgrade failure'),
        ]
        for error in errors:
            with self.subTest(error=error):
                middleware = Mock()
                service = PoolService(middleware)
                context = service.pool_extend_context([], {'is_upgraded': True})
                middleware.call_sync.side_effect = [normalized_pool('ONLINE'), error]

                with self.assertRaises(type(error)) as caught:
                    service.pool_extend(pool_row(), context)

                self.assertIs(caught.exception, error)
                self.assertEqual(middleware.call_sync.call_count, 2)

    def test_failed_export_race_recheck_does_not_report_offline_success(self):
        middleware = Mock()
        service = PoolService(middleware)
        context = service.pool_extend_context([], {'is_upgraded': True})
        recheck_error = CallError('pool state unavailable', errno.EIO)
        middleware.call_sync.side_effect = [
            normalized_pool('ONLINE'),
            ValidationError('pool.is_upgraded', 'pool is not imported', errno.ENOENT),
            recheck_error,
        ]

        with self.assertRaises(CallError) as caught:
            service.pool_extend(pool_row(), context)

        self.assertIs(caught.exception, recheck_error)
        self.assertEqual(middleware.call_sync.call_count, 3)

    def test_direct_is_upgraded_still_rejects_a_missing_pool(self):
        middleware = Mock()
        middleware.call_sync.return_value = []

        with self.assertRaises(ValidationError) as caught:
            PoolInfoService(middleware).is_upgraded(Mock(), 7)

        self.assertEqual(caught.exception.errno, errno.ENOENT)
        middleware.call_sync.assert_called_once_with('datastore.query', 'storage.volume', [['id', '=', 7]])


if __name__ == '__main__':
    unittest.main()

